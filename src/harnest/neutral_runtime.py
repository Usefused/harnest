"""Framework-neutral HTTP, SSE, and WebSocket runtime.

Backend drivers translate framework events into the deliberately small internal
event vocabulary in this module.  Everything that is part of Harnest's public
wire protocol lives here so ADK and LangGraph cannot drift independently.
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field
import json
from typing import Any, AsyncIterator, Mapping, Protocol, Sequence, runtime_checkable
import uuid

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    create_model,
    field_validator,
)
from starlette.requests import Request
from starlette.responses import Response
from starlette.exceptions import HTTPException
from starlette.websockets import WebSocket, WebSocketDisconnect

from .runtime_auth import (
    ANONYMOUS_USER_ID,
    Authenticator,
    install_authentication,
    principal_for,
)
from .approval import (
    ApprovalDenied,
    ApprovalEnforcementError,
    ApprovalExecution,
    ApprovalExpired,
    ApprovalRun,
    InMemoryApprovalStore,
    PendingApproval,
    approval_execution,
)
from .client_tool import (
    ClientToolError,
    ClientToolExecution,
    InMemoryClientToolStore,
    PendingClientTool,
    client_tool_execution,
)
from .server_config import format_byte_size, validate_max_request_bytes


MAX_REQUEST_BYTES = 1024 * 1024
MAX_LIVE_FRAMES = 1024
NEUTRAL_USER_ID = ANONYMOUS_USER_ID

RuntimeEvent = dict[str, Any]


class _ResponseEnvelope(BaseModel):
    """Fields shared by text and user-configured response request models."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    session_id: str | None = Field(default=None, alias="sessionId")
    stream: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("session_id")
    @classmethod
    def _non_empty_session_id(cls, value: str | None) -> str | None:
        """Keep explicitly selected sessions unambiguous."""

        if value is not None and not value.strip():
            raise ValueError("sessionId must be non-empty")
        return value


class ResponseRequest(_ResponseEnvelope):
    """Strict Pydantic body accepted by default by ``POST /responses``."""

    input: str

    @field_validator("input")
    @classmethod
    def _non_empty_input(cls, value: str) -> str:
        """Reject input that cannot produce a meaningful model turn."""

        if not value.strip():
            raise ValueError("input must be non-empty")
        return value


def _response_request_model(input_schema: Any) -> type[BaseModel]:
    """Create the transport envelope around one authored Pydantic input model."""

    if input_schema is None:
        return ResponseRequest
    # A per-application model makes the user's contract visible under `input`
    # in OpenAPI without turning session and streaming controls into model data.
    return create_model(
        "ResponseRequest",
        __base__=_ResponseEnvelope,
        input=(input_schema, ...),
    )


@dataclass(frozen=True, slots=True)
class AgentInfo:
    """Portable metadata needed by the neutral server."""

    id: str
    name: str
    description: str
    card: Mapping[str, Any]
    framework: str | None = None
    mode: str | None = None
    extra_endpoints: Mapping[str, str] = field(default_factory=dict)
    input_schema: Any = None
    output_schema: Any = None


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """A backend-independent view of a persisted agent session."""

    id: str
    user_id: str
    state: Mapping[str, Any]
    created_at: str | None = None
    updated_at: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SessionMessage:
    """One portable transcript item with lossless framework-owned metadata."""

    id: str
    role: str
    content: Any
    created_at: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InvocationRequest:
    """The complete input passed from a transport to a runtime driver."""

    input: Any
    user_id: str
    session_id: str
    invocation_id: str
    metadata: Mapping[str, Any]
    state_delta: Mapping[str, Any]
    transport: str | None = None


@dataclass(frozen=True, slots=True)
class InvocationResult:
    """The normalized result of a non-streaming invocation."""

    text: str
    events: Sequence[RuntimeEvent]
    result: Any
    session_id: str
    metadata: Mapping[str, Any]


@runtime_checkable
class RuntimeDriver(Protocol):
    """The only interface the neutral server requires from a framework."""

    @property
    def info(self) -> AgentInfo: ...

    async def create_session(
        self,
        *,
        session_id: str,
        user_id: str,
        state: Mapping[str, Any],
    ) -> SessionRecord: ...

    async def get_session(
        self, *, session_id: str, user_id: str
    ) -> SessionRecord | None: ...

    async def list_sessions(self, *, user_id: str) -> Sequence[SessionRecord]: ...

    async def get_session_messages(
        self, *, session_id: str, user_id: str
    ) -> Sequence[SessionMessage] | None: ...

    async def update_session(
        self,
        *,
        session_id: str,
        user_id: str,
        state_delta: Mapping[str, Any],
    ) -> SessionRecord | None: ...

    async def delete_session(self, *, session_id: str, user_id: str) -> bool: ...

    async def invoke(self, request: InvocationRequest) -> InvocationResult: ...

    def stream(self, request: InvocationRequest) -> AsyncIterator[RuntimeEvent]: ...

    async def close(self) -> None: ...


class SessionConflictError(RuntimeError):
    """Raised by a driver when a requested session id already exists."""


class NoCustomerFacingOutputError(RuntimeError):
    """Raised when a completed invocation contains no public answer or result."""


def require_customer_facing_output(text: str, result: Any) -> None:
    """Reject reasoning-only completions without exposing their hidden content."""

    if text.strip() or result is not None:
        return
    # Returning an empty successful response makes provider failures look like
    # valid agent behavior. The error remains provider-neutral and keeps hidden
    # chain-of-thought out of every public transport.
    raise NoCustomerFacingOutputError(
        "Agent completed without customer-facing output"
    )


def _session_payload(session: SessionRecord) -> dict[str, Any]:
    """Render the complete neutral record without flattening native metadata."""

    return {
        "id": session.id,
        "userId": session.user_id,
        "state": dict(session.state),
        "createdAt": session.created_at,
        "updatedAt": session.updated_at,
        "metadata": dict(session.metadata),
    }


def _session_message_payload(message: SessionMessage) -> dict[str, Any]:
    """Render one stable transcript item without flattening native metadata."""

    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "createdAt": message.created_at,
        "metadata": dict(message.metadata),
    }


def _public_output_item(event: RuntimeEvent) -> dict[str, Any]:
    event_type = event.get("type")
    if event_type == "message":
        return {
            "type": "message",
            "role": event.get("role", "assistant"),
            "content": [{"type": "output_text", "text": event.get("text", "")}],
        }
    if event_type == "tool_call":
        return {
            "type": "tool_call",
            "id": event.get("id"),
            "name": event.get("name"),
            "arguments": event.get("arguments"),
        }
    if event_type == "tool_result":
        return {
            "type": "tool_result",
            "callId": event.get("id", event.get("callId")),
            "name": event.get("name"),
            "output": event.get("result", event.get("output")),
        }
    if event_type == "graph_output":
        return {"type": "output", "value": event.get("output")}
    if event_type == "output":
        return {"type": "output", "value": event.get("value")}
    raise ValueError(f"unsupported runtime event type: {event_type!r}")


def _public_output(events: Sequence[RuntimeEvent]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for event in events:
        item = _public_output_item(event)
        if item["type"] == "message" and output and output[-1]["type"] == "message":
            output[-1]["content"][0]["text"] += item["content"][0]["text"]
        else:
            output.append(item)
    return output


def _stream_frame(
    event: RuntimeEvent,
    *,
    sequence: int,
    response_id: str,
    session_id: str,
    request_id: str | None = None,
) -> tuple[str, dict[str, Any]] | None:
    common: dict[str, Any] = {
        "sequence": sequence,
        "responseId": response_id,
        "sessionId": session_id,
    }
    if request_id is not None:
        common["requestId"] = request_id
    event_type = event.get("type")
    if event_type == "message":
        return "response.text.delta", {
            **common,
            "type": "response.text.delta",
            "delta": event.get("text", ""),
        }
    if event_type == "tool_call":
        return "response.tool_call", {
            **common,
            "type": "response.tool_call",
            "id": event.get("id"),
            "name": event.get("name"),
            "arguments": event.get("arguments"),
        }
    if event_type == "tool_result":
        return "response.tool_result", {
            **common,
            "type": "response.tool_result",
            "callId": event.get("id", event.get("callId")),
            "name": event.get("name"),
            "output": event.get("result", event.get("output")),
        }
    # Graph/output events belong in the completed response, not a transport-
    # specific incremental frame.
    if event_type in {"graph_output", "output"}:
        return None
    raise ValueError(f"unsupported runtime event type: {event_type!r}")


def _completed_payload(
    *,
    response_id: str,
    session_id: str,
    sequence: int,
    events: Sequence[RuntimeEvent],
    text: str,
    metadata: Mapping[str, Any],
    result: Any = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "response.completed",
        "sequence": sequence,
        "responseId": response_id,
        "sessionId": session_id,
        "status": "completed",
        "outputText": text,
        "output": _public_output(events),
        "metadata": dict(metadata),
    }
    if request_id is not None:
        payload["requestId"] = request_id
    if result is not None:
        payload["result"] = result
    return payload


def _approval_payload(
    pending: PendingApproval,
    *,
    response_id: str,
    session_id: str,
    sequence: int,
    request_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "approval.requested",
        "sequence": sequence,
        "responseId": response_id,
        "sessionId": session_id,
        "approval": pending.public(),
    }
    if request_id is not None:
        payload["requestId"] = request_id
    return payload


def _requires_action_payload(
    pending: PendingApproval,
    *,
    response_id: str,
    session_id: str,
    sequence: int,
    request_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "response.completed",
        "sequence": sequence,
        "responseId": response_id,
        "sessionId": session_id,
        "status": "requires_action",
        "requiredAction": {"type": "human_approval", **pending.public()},
        "outputText": "",
        "output": [],
        "metadata": {},
    }
    if request_id is not None:
        payload["requestId"] = request_id
    return payload


def _client_tool_payload(
    pending: PendingClientTool,
    *,
    response_id: str,
    session_id: str,
    sequence: int,
    request_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "client_tool.requested",
        "sequence": sequence,
        "responseId": response_id,
        "sessionId": session_id,
        "clientTool": pending.public(),
    }
    if request_id is not None:
        payload["requestId"] = request_id
    return payload


def _client_requires_action_payload(
    pending: PendingClientTool,
    *,
    response_id: str,
    session_id: str,
    sequence: int,
) -> dict[str, Any]:
    return {
        "type": "response.completed",
        "sequence": sequence,
        "responseId": response_id,
        "sessionId": session_id,
        "status": "requires_action",
        "requiredAction": {"type": "client_tool", **pending.public()},
        "outputText": "",
        "output": [],
        "metadata": {},
    }


def _start_approval_run(
    store: InMemoryApprovalStore,
    client_tools: InMemoryClientToolStore,
    driver: RuntimeDriver,
    request: InvocationRequest,
    *,
    stream: bool,
) -> ApprovalRun:
    run = store.create_run(
        user_id=request.user_id,
        session_id=request.session_id,
        call_id=request.invocation_id,
    )

    async def execute() -> None:
        await run.activation.wait()
        try:
            with client_tool_execution(ClientToolExecution(client_tools, run)):
                with approval_execution(
                    ApprovalExecution(
                        user_id=request.user_id,
                        session_id=request.session_id,
                        call_id=request.invocation_id,
                        store=store,
                        run=run,
                    )
                ):
                    result = (
                        await _collect_stream(driver, request, run)
                        if stream
                        else await driver.invoke(request)
                    )
        except BaseException as exc:
            run.notifications.put_nowait(("error", exc))
        else:
            run.notifications.put_nowait(("result", result))

    run.task = asyncio.create_task(execute())
    return run


async def _collect_stream(
    driver: RuntimeDriver, request: InvocationRequest, run: ApprovalRun
) -> InvocationResult:
    events: list[RuntimeEvent] = []
    async with aclosing(driver.stream(request)) as source:
        async for event in source:
            events.append(event)
            run.notifications.put_nowait(("event", event))
    text = "".join(
        str(event.get("text", ""))
        for event in events
        if event.get("type") == "message"
    )
    return InvocationResult(
        text=text,
        events=tuple(events),
        result=_final_event_result(events),
        session_id=request.session_id,
        metadata=dict(request.metadata),
    )


async def _next_run_boundary(
    run: ApprovalRun, *, timeout: float
) -> tuple[str, Any]:
    return await asyncio.wait_for(run.notifications.get(), timeout=timeout)


async def _next_non_event(run: ApprovalRun, *, deadline: float) -> tuple[str, Any]:
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        kind, value = await _next_run_boundary(run, timeout=remaining)
        if kind != "event":
            return kind, value


def _json_requires_action(
    pending: PendingApproval, *, response_id: str, session_id: str
) -> dict[str, Any]:
    required = _requires_action_payload(
        pending,
        response_id=response_id,
        session_id=session_id,
        sequence=0,
    )
    required.pop("type")
    required.pop("sequence")
    required["id"] = required.pop("responseId")
    return required


def _json_client_requires_action(
    pending: PendingClientTool, *, response_id: str, session_id: str
) -> dict[str, Any]:
    required = _client_requires_action_payload(
        pending,
        response_id=response_id,
        session_id=session_id,
        sequence=0,
    )
    required.pop("type")
    required.pop("sequence")
    required["id"] = required.pop("responseId")
    return required


def _resumed_action_payload(
    kind: str,
    value: Any,
    *,
    response_id: str,
    session_id: str,
    client_tool_id: str,
) -> dict[str, Any]:
    if kind == "approval":
        payload = _json_requires_action(
            value, response_id=response_id, session_id=session_id
        )
        payload["approvalId"] = value.id
        return payload
    if kind == "client_tool":
        return _json_client_requires_action(
            value, response_id=response_id, session_id=session_id
        )
    if kind == "error":
        raise value
    if kind != "result":
        raise RuntimeError("unexpected runtime action notification")
    require_customer_facing_output(value.text, value.result)
    payload = _completed_payload(
        response_id=response_id,
        session_id=value.session_id,
        sequence=0,
        events=value.events,
        text=value.text,
        metadata=value.metadata,
        result=value.result,
    )
    payload.pop("type")
    payload.pop("sequence")
    payload["id"] = payload.pop("responseId")
    payload["clientToolId"] = client_tool_id
    return payload


async def _sse_approval_run(
    *,
    store: InMemoryApprovalStore,
    client_tools: InMemoryClientToolStore,
    driver: RuntimeDriver,
    request: InvocationRequest,
    semaphore: asyncio.Semaphore,
    request_timeout: float,
    response_id: str,
    session_id: str,
    metadata: Mapping[str, Any],
) -> AsyncIterator[str]:
    sequence = 0
    yield _sse(
        "response.created",
        {
            "type": "response.created",
            "sequence": sequence,
            "responseId": response_id,
            "sessionId": session_id,
            "metadata": dict(metadata),
        },
    )
    sequence += 1
    run = _start_approval_run(store, client_tools, driver, request, stream=True)
    deadline = asyncio.get_running_loop().time() + request_timeout
    try:
        async with semaphore:
            run.activation.set()
            async for frame in _sse_run_frames(
                run,
                deadline=deadline,
                response_id=response_id,
                session_id=session_id,
                start_sequence=sequence,
                metadata=metadata,
            ):
                yield frame
    except asyncio.CancelledError:
        store.cancel_run(run)
        raise
    except Exception as exc:
        if isinstance(exc, asyncio.TimeoutError):
            store.cancel_run(run)
        yield _sse(
            "error",
            {
                "type": "error",
                "sequence": sequence,
                "responseId": response_id,
                "sessionId": session_id,
                "error": "Response timed out"
                if isinstance(exc, asyncio.TimeoutError)
                else str(exc),
            },
        )


async def _sse_run_frames(
    run: ApprovalRun,
    *,
    deadline: float,
    response_id: str,
    session_id: str,
    start_sequence: int,
    metadata: Mapping[str, Any],
) -> AsyncIterator[str]:
    sequence = start_sequence
    events: list[RuntimeEvent] = []
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        kind, value = await _next_run_boundary(run, timeout=remaining)
        if kind == "event":
            events.append(value)
            frame = _stream_frame(
                value,
                sequence=sequence,
                response_id=response_id,
                session_id=session_id,
            )
            if frame is not None:
                yield _sse(*frame)
                sequence += 1
            continue
        if kind == "approval":
            yield _sse(
                "approval.requested",
                _approval_payload(
                    value,
                    response_id=response_id,
                    session_id=session_id,
                    sequence=sequence,
                ),
            )
            yield _sse(
                "response.completed",
                _requires_action_payload(
                    value,
                    response_id=response_id,
                    session_id=session_id,
                    sequence=sequence + 1,
                ),
            )
            return
        if kind == "client_tool":
            yield _sse(
                "client_tool.requested",
                _client_tool_payload(
                    value,
                    response_id=response_id,
                    session_id=session_id,
                    sequence=sequence,
                ),
            )
            yield _sse(
                "response.completed",
                _client_requires_action_payload(
                    value,
                    response_id=response_id,
                    session_id=session_id,
                    sequence=sequence + 1,
                ),
            )
            return
        if kind == "error":
            raise value
        if kind != "result":
            raise RuntimeError("unexpected approval run notification")
        require_customer_facing_output(value.text, value.result)
        yield _sse(
            "response.completed",
            _completed_payload(
                response_id=response_id,
                session_id=session_id,
                sequence=sequence,
                events=events,
                text=value.text,
                metadata=metadata,
                result=value.result,
            ),
        )
        return


async def _resume_approval_run(
    store: InMemoryApprovalStore,
    pending: PendingApproval,
    run: ApprovalRun,
    *,
    semaphore: asyncio.Semaphore,
    request_timeout: float,
) -> tuple[str, Any]:
    async with semaphore:
        store.deliver_decision(pending, "approve")
        deadline = asyncio.get_running_loop().time() + request_timeout
        return await _next_non_event(run, deadline=deadline)


def _parse_approval_decision(payload: Mapping[str, Any]) -> str:
    if set(payload) != {"decision"}:
        raise HTTPException(status_code=400, detail="Expected approval decision")
    decision = payload.get("decision")
    if decision not in {"approve", "deny"}:
        raise HTTPException(status_code=400, detail="decision must be approve or deny")
    return decision


def _commit_approval_decision(
    store: InMemoryApprovalStore,
    approval_id: str,
    *,
    user_id: str,
    decision: str,
) -> PendingApproval:
    try:
        return store.decide(
            approval_id,
            user_id=user_id,
            decision=decision,  # type: ignore[arg-type]
            deliver=decision == "deny",
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Approval not found") from exc
    except ApprovalEnforcementError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _final_event_result(events: Sequence[RuntimeEvent]) -> Any:
    """Read the portable result carried by the final output event."""

    for event in reversed(events):
        event_type = event.get("type")
        if event_type not in {"graph_output", "output"}:
            continue
        if "result" in event:
            return event["result"]
        return event.get("output") if event_type == "graph_output" else event.get("value")
    return None


def _sse(event_name: str, payload: Mapping[str, Any]) -> str:
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@dataclass(slots=True)
class _LiveStreamState:
    """Retain sequence progress so mid-stream errors keep the wire contract."""

    events: list[RuntimeEvent] = field(default_factory=list)
    sequence: int = 1


async def _consume_live_run(
    websocket: Any,
    run: ApprovalRun,
    state: _LiveStreamState,
    *,
    client_tools: InMemoryClientToolStore,
    deadline: float,
    response_id: str,
    session_id: str,
    request_id: str | None,
) -> InvocationResult | None:
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        kind, value = await _next_run_boundary(run, timeout=remaining)
        if kind == "event":
            state.events.append(value)
            normalized = _stream_frame(
                value,
                sequence=state.sequence,
                response_id=response_id,
                session_id=session_id,
                request_id=request_id,
            )
            if normalized is not None:
                await websocket.send_json(normalized[1])
                state.sequence += 1
            continue
        if kind == "approval":
            await websocket.send_json(
                _approval_payload(
                    value,
                    response_id=response_id,
                    session_id=session_id,
                    sequence=state.sequence,
                    request_id=request_id,
                )
            )
            await websocket.send_json(
                _requires_action_payload(
                    value,
                    response_id=response_id,
                    session_id=session_id,
                    sequence=state.sequence + 1,
                    request_id=request_id,
                )
            )
            return None
        if kind == "client_tool":
            await websocket.send_json(
                _client_tool_payload(
                    value,
                    response_id=response_id,
                    session_id=session_id,
                    sequence=state.sequence,
                    request_id=request_id,
                )
            )
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            frame = await asyncio.wait_for(
                websocket.receive_json(), timeout=remaining
            )
            output = _live_client_tool_result(frame, value.id)
            client_tools.submit(value.id, user_id=value.user_id, output=output)
            state.sequence += 1
            continue
        if kind == "error":
            raise value
        if kind == "result":
            return value
        raise RuntimeError("unexpected approval run notification")


def _live_client_tool_result(frame: Any, request_id: str) -> Any:
    if (
        not isinstance(frame, dict)
        or set(frame) != {"type", "requestId", "output"}
        or frame.get("type") != "client_tool.result"
        or frame.get("requestId") != request_id
    ):
        raise ClientToolError(
            "Expected client_tool.result for the pending client tool request"
        )
    return frame["output"]


async def _live_session(websocket: Any, response_session: Any) -> SessionRecord | None:
    envelope = await websocket.receive_json()
    if (
        not isinstance(envelope, dict)
        or not set(envelope) <= {"type", "sessionId"}
        or envelope.get("type") != "connect"
    ):
        await websocket.send_json(
            {"type": "error", "error": "First frame must be a connect request"}
        )
        await websocket.close(code=1008)
        return None
    try:
        session = await response_session(envelope)
    except HTTPException as exc:
        await websocket.send_json({"type": "error", "error": exc.detail})
        await websocket.close(code=1008)
        return None
    await websocket.send_json({"type": "session.connected", "sessionId": session.id})
    return session


def _validated_live_frame(
    frame: Any,
    max_request_bytes: int = MAX_REQUEST_BYTES,
    input_schema: Any = None,
) -> tuple[Mapping[str, Any] | None, str | None]:
    """Validate and normalize one WebSocket request with the HTTP input contract."""

    if (
        not isinstance(frame, dict)
        or not set(frame) <= {"type", "input", "requestId", "metadata"}
        or frame.get("type") != "response.create"
    ):
        return None, "Invalid live input frame"
    normalized_input = _validated_live_input(frame.get("input"), input_schema)
    if normalized_input is None:
        return None, "Invalid live input frame"
    encoded_input = json.dumps(normalized_input, ensure_ascii=False).encode("utf-8")
    if len(encoded_input) > max_request_bytes:
        return None, f"Input exceeds {format_byte_size(max_request_bytes)}"
    if not isinstance(frame.get("metadata", {}), dict):
        return None, "metadata must be an object"
    request_id = frame.get("requestId")
    if request_id is not None and not isinstance(request_id, str):
        return None, "requestId must be a string"
    return {**frame, "input": normalized_input}, None


def _validated_live_input(value: Any, input_schema: Any) -> Any | None:
    """Apply the configured input model without exposing validation details."""

    if input_schema is None:
        return value if isinstance(value, str) and value.strip() else None
    try:
        return input_schema.model_validate(value).model_dump(
            mode="json", by_alias=True
        )
    except (ValidationError, ValueError, TypeError):
        return None


async def _serve_live_frame(
    websocket: Any,
    frame: Mapping[str, Any],
    session: SessionRecord,
    *,
    invocation: Any,
    semaphore: asyncio.Semaphore,
    driver: RuntimeDriver,
    approval_store: InMemoryApprovalStore,
    client_tool_store: InMemoryClientToolStore,
    request_timeout: float,
) -> None:
    response_id = f"resp_{uuid.uuid4().hex}"
    request_id = frame.get("requestId")
    metadata = frame.get("metadata", {})
    await websocket.send_json(
        {
            "type": "response.created",
            "sequence": 0,
            "responseId": response_id,
            "sessionId": session.id,
            "requestId": request_id,
            "metadata": metadata,
        }
    )
    run = invocation(
        frame["input"],
        session.id,
        response_id,
        metadata,
        session.user_id,
        transport="live",
    )
    state = _LiveStreamState()
    approval_run = _start_approval_run(
        approval_store, client_tool_store, driver, run, stream=True
    )
    deadline = asyncio.get_running_loop().time() + request_timeout
    try:
        async with semaphore:
            approval_run.activation.set()
            result = await _consume_live_run(
                websocket,
                approval_run,
                state,
                client_tools=client_tool_store,
                deadline=deadline,
                response_id=response_id,
                session_id=session.id,
                request_id=request_id,
            )
            if result is None:
                return
        text_output = result.text
        result_value = result.result
        require_customer_facing_output(text_output, result_value)
        await websocket.send_json(
            _completed_payload(
                response_id=response_id,
                session_id=session.id,
                sequence=state.sequence,
                events=state.events,
                text=text_output,
                metadata=metadata,
                result=result_value,
                request_id=request_id,
            )
        )
    except asyncio.CancelledError:
        approval_store.cancel_run(approval_run)
        raise
    except Exception as exc:
        if isinstance(exc, asyncio.TimeoutError):
            approval_store.cancel_run(approval_run)
        await websocket.send_json(
            {
                "type": "error",
                "sequence": state.sequence,
                "responseId": response_id,
                "sessionId": session.id,
                "requestId": request_id,
                "error": (
                    "Response timed out"
                    if isinstance(exc, asyncio.TimeoutError)
                    else str(exc)
                ),
            }
        )


async def _read_request_body(request: Request, max_request_bytes: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_request_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    "Request body exceeds "
                    f"{format_byte_size(max_request_bytes)}"
                ),
            )
        chunks.append(chunk)
    return b"".join(chunks)


def create_neutral_router(
    driver: RuntimeDriver,
    *,
    request_timeout: float = 300,
    max_concurrency: int = 8,
    max_request_bytes: int = MAX_REQUEST_BYTES,
    approval_store: InMemoryApprovalStore | None = None,
    client_tool_store: InMemoryClientToolStore | None = None,
) -> Any:
    """Create the one Harnest router shared by every runtime backend."""

    if request_timeout <= 0:
        raise ValueError("request timeout must be greater than zero")
    if max_concurrency < 1:
        raise ValueError("max concurrency must be at least one")
    max_request_bytes = validate_max_request_bytes(max_request_bytes)
    try:
        from fastapi import APIRouter, HTTPException
        from fastapi.responses import StreamingResponse
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError("The neutral runtime requires FastAPI") from exc

    router = APIRouter()
    semaphore = asyncio.Semaphore(max_concurrency)
    approvals = approval_store or InMemoryApprovalStore()
    client_tools = client_tool_store or InMemoryClientToolStore()
    response_request_model = _response_request_model(driver.info.input_schema)

    async def read_json(request: Request) -> dict[str, Any]:
        content_type = request.headers.get("content-type", "").partition(";")[0]
        if content_type.strip().lower() != "application/json":
            raise HTTPException(
                status_code=415, detail="Content-Type must be application/json"
            )
        body = await _read_request_body(request, max_request_bytes)
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise HTTPException(status_code=400, detail="Expected a JSON object")
        return value

    def parse_response(payload: Mapping[str, Any]) -> BaseModel:
        """Validate the public envelope without changing established HTTP errors."""

        try:
            parsed = response_request_model.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid response request"
            ) from exc
        input_value = parsed.input
        if isinstance(input_value, str) and len(input_value.encode("utf-8")) > max_request_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Input exceeds {format_byte_size(max_request_bytes)}",
            )
        return parsed

    async def response_session(
        payload: Mapping[str, Any], user_id: str
    ) -> SessionRecord:
        session_id = payload.get("sessionId")
        if session_id is not None and (
            not isinstance(session_id, str) or not session_id.strip()
        ):
            raise HTTPException(status_code=400, detail="sessionId must be non-empty")
        if session_id is None:
            return await driver.create_session(
                session_id=uuid.uuid4().hex, user_id=user_id, state={}
            )
        session = await driver.get_session(
            session_id=session_id, user_id=user_id
        )
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return session

    def invocation(
        text: str,
        session_id: str,
        response_id: str,
        metadata: Mapping[str, Any],
        user_id: str,
        transport: str | None = None,
    ) -> InvocationRequest:
        return InvocationRequest(
            input=text,
            user_id=user_id,
            session_id=session_id,
            invocation_id=response_id,
            metadata=dict(metadata),
            state_delta={},
            transport=transport,
        )

    @router.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/.well-known/agent-card.json", include_in_schema=False)
    async def agent_card() -> Mapping[str, Any]:
        return driver.info.card

    @router.get("/agent")
    async def agent_info() -> dict[str, Any]:
        info = driver.info
        value: dict[str, Any] = {
            "id": info.id,
            "name": info.name,
            "description": info.description,
            "card": dict(info.card),
            "endpoints": {
                "responses": "/responses",
                "sessions": "/sessions",
                "sessionMessages": "/sessions/{sessionId}/messages",
                "live": "/live",
                "approvals": "/approvals/{approvalId}",
                "clientTools": "/client-tools/{requestId}",
                **dict(info.extra_endpoints),
            },
        }
        if info.framework is not None:
            value["framework"] = info.framework
        if info.mode is not None:
            value["mode"] = info.mode
        return value

    @router.post("/sessions", status_code=201)
    async def create_session(request: Request) -> dict[str, Any]:
        payload = await read_json(request)
        if not set(payload) <= {"id", "state"}:
            raise HTTPException(status_code=400, detail="Invalid session request")
        session_id = payload.get("id", uuid.uuid4().hex)
        state = payload.get("state", {})
        if not isinstance(session_id, str) or not session_id.strip():
            raise HTTPException(status_code=400, detail="id must be non-empty")
        if not isinstance(state, dict):
            raise HTTPException(status_code=400, detail="state must be an object")
        try:
            session = await driver.create_session(
                session_id=session_id,
                user_id=principal_for(request).user_id,
                state=state,
            )
        except SessionConflictError as exc:
            raise HTTPException(status_code=409, detail="Session already exists") from exc
        return _session_payload(session)

    @router.get("/sessions")
    async def list_sessions(request: Request) -> dict[str, Any]:
        sessions = await driver.list_sessions(
            user_id=principal_for(request).user_id
        )
        return {
            "sessions": [
                _session_payload(session)
                for session in sorted(sessions, key=lambda item: item.id)
            ]
        }

    @router.get("/sessions/{session_id}")
    async def get_session(session_id: str, request: Request) -> dict[str, Any]:
        session = await driver.get_session(
            session_id=session_id,
            user_id=principal_for(request).user_id,
        )
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return _session_payload(session)

    @router.get("/sessions/{session_id}/messages")
    async def get_session_messages(
        session_id: str, request: Request
    ) -> dict[str, Any]:
        """Return a self-describing transcript within the caller's scope."""

        principal = principal_for(request)
        messages = await driver.get_session_messages(
            session_id=session_id,
            user_id=principal.user_id,
        )
        if messages is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return {
            "sessionId": session_id,
            "userId": principal.user_id,
            "messages": [_session_message_payload(message) for message in messages],
        }

    @router.patch("/sessions/{session_id}")
    async def update_session(session_id: str, request: Request) -> dict[str, Any]:
        payload = await read_json(request)
        if set(payload) != {"stateDelta"} or not isinstance(
            payload.get("stateDelta"), dict
        ):
            raise HTTPException(status_code=400, detail="Expected stateDelta object")
        session = await driver.update_session(
            session_id=session_id,
            user_id=principal_for(request).user_id,
            state_delta=payload["stateDelta"],
        )
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return _session_payload(session)

    @router.delete("/sessions/{session_id}", status_code=204)
    async def delete_session(session_id: str, request: Request) -> Response:
        deleted = await driver.delete_session(
            session_id=session_id,
            user_id=principal_for(request).user_id,
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="Session not found")
        return Response(status_code=204)

    @router.post(
        "/responses",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": response_request_model.model_json_schema(by_alias=True)
                    }
                },
            }
        },
    )
    async def responses(request: Request) -> Any:
        payload = await read_json(request)
        parsed = parse_response(payload)
        text = parsed.input
        if driver.info.input_schema is not None:
            text = text.model_dump(mode="json", by_alias=True)
        stream, metadata = parsed.stream, parsed.metadata
        user_id = principal_for(request).user_id
        session = await response_session(payload, user_id)
        response_id = f"resp_{uuid.uuid4().hex}"
        run = invocation(
            text,
            session.id,
            response_id,
            metadata,
            user_id,
            transport="stream" if stream else "response",
        )
        if not stream:
            approval_run = _start_approval_run(
                approvals, client_tools, driver, run, stream=False
            )
            try:
                async with semaphore:
                    approval_run.activation.set()
                    kind, value = await _next_run_boundary(
                        approval_run, timeout=request_timeout
                    )
                    if kind == "approval":
                        required = _requires_action_payload(
                            value,
                            response_id=response_id,
                            session_id=session.id,
                            sequence=0,
                        )
                        required.pop("type")
                        required.pop("sequence")
                        required["id"] = required.pop("responseId")
                        return required
                    if kind == "client_tool":
                        required = _client_requires_action_payload(
                            value,
                            response_id=response_id,
                            session_id=session.id,
                            sequence=0,
                        )
                        required.pop("type")
                        required.pop("sequence")
                        required["id"] = required.pop("responseId")
                        return required
                    if kind == "error":
                        raise value
                    if kind != "result":
                        raise RuntimeError("unexpected approval run notification")
                    result = value
                    require_customer_facing_output(result.text, result.result)
            except asyncio.TimeoutError as exc:
                approvals.cancel_run(approval_run)
                raise HTTPException(status_code=504, detail="Response timed out") from exc
            except NoCustomerFacingOutputError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            completed = _completed_payload(
                response_id=response_id,
                session_id=result.session_id,
                sequence=0,
                events=result.events,
                text=result.text,
                metadata=result.metadata,
                result=result.result,
            )
            completed.pop("type")
            completed.pop("sequence")
            completed["id"] = completed.pop("responseId")
            return completed

        return StreamingResponse(
            _sse_approval_run(
                store=approvals,
                client_tools=client_tools,
                driver=driver,
                request=run,
                semaphore=semaphore,
                request_timeout=request_timeout,
                response_id=response_id,
                session_id=session.id,
                metadata=metadata,
            ),
            media_type="text/event-stream",
        )

    @router.post("/client-tools/{tool_request_id}")
    async def submit_client_tool(tool_request_id: str, request: Request) -> Any:
        payload = await read_json(request)
        if set(payload) != {"output"}:
            raise HTTPException(status_code=400, detail="Expected client tool output")
        try:
            pending = client_tools.submit(
                tool_request_id,
                user_id=principal_for(request).user_id,
                output=payload["output"],
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Client tool request not found") from exc
        except ClientToolError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            deadline = asyncio.get_running_loop().time() + request_timeout
            async with semaphore:
                kind, value = await _next_non_event(pending.run, deadline=deadline)
            return _resumed_action_payload(
                kind,
                value,
                response_id=pending.call_id,
                session_id=pending.session_id,
                client_tool_id=pending.id,
            )
        except asyncio.TimeoutError as exc:
            approvals.cancel_run(pending.run)
            raise HTTPException(status_code=504, detail="Response timed out") from exc

    @router.post("/approvals/{approval_id}")
    async def decide_approval(approval_id: str, request: Request) -> Any:
        payload = await read_json(request)
        decision = _parse_approval_decision(payload)
        pending = _commit_approval_decision(
            approvals,
            approval_id,
            user_id=principal_for(request).user_id,
            decision=decision,
        )
        if decision == "deny":
            return {"id": pending.id, "status": "denied"}
        approval_run = approvals.run_for(pending)
        if approval_run is None:
            raise HTTPException(status_code=409, detail="Approval execution is unavailable")
        try:
            kind, value = await _resume_approval_run(
                approvals,
                pending,
                approval_run,
                semaphore=semaphore,
                request_timeout=request_timeout,
            )
            if kind == "approval":
                required = _json_requires_action(
                    value,
                    response_id=pending.call_id,
                    session_id=pending.session_id,
                )
                required["approvalId"] = value.id
                return required
            if kind == "client_tool":
                return _json_client_requires_action(
                    value,
                    response_id=pending.call_id,
                    session_id=pending.session_id,
                )
            if kind == "error":
                raise value
            if kind != "result":
                raise RuntimeError("unexpected approval run notification")
            result = value
            require_customer_facing_output(result.text, result.result)
        except asyncio.CancelledError:
            approvals.cancel_run(approval_run)
            raise
        except (ApprovalDenied, ApprovalExpired, ApprovalEnforcementError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except asyncio.TimeoutError as exc:
            approvals.cancel_run(approval_run)
            raise HTTPException(status_code=504, detail="Response timed out") from exc
        completed = _completed_payload(
            response_id=pending.call_id,
            session_id=result.session_id,
            sequence=0,
            events=result.events,
            text=result.text,
            metadata=result.metadata,
            result=result.result,
        )
        completed.pop("type")
        completed.pop("sequence")
        completed["id"] = completed.pop("responseId")
        completed["approvalId"] = pending.id
        return completed

    @router.websocket("/live")
    async def live(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            user_id = principal_for(websocket).user_id
            session = await _live_session(
                websocket,
                lambda payload: response_session(payload, user_id),
            )
            if session is None:
                return
            frame_count = 0
            while True:
                frame = await websocket.receive_json()
                frame_count += 1
                if frame_count > MAX_LIVE_FRAMES:
                    await websocket.close(code=1008, reason="Frame limit exceeded")
                    return
                if frame == {"type": "session.close"}:
                    await websocket.close(code=1000)
                    return
                validated_frame, error = _validated_live_frame(
                    frame,
                    max_request_bytes,
                    driver.info.input_schema,
                )
                if error is not None:
                    await websocket.send_json({"type": "error", "error": error})
                    continue
                await _serve_live_frame(
                    websocket,
                    validated_frame,
                    session,
                    invocation=invocation,
                    semaphore=semaphore,
                    driver=driver,
                    approval_store=approvals,
                    client_tool_store=client_tools,
                    request_timeout=request_timeout,
                )
        except WebSocketDisconnect:
            pass

    return router


def create_neutral_app(
    driver: RuntimeDriver,
    *,
    request_timeout: float = 300,
    max_concurrency: int = 8,
    max_request_bytes: int = MAX_REQUEST_BYTES,
    playground_enabled: bool = True,
    authenticator: Authenticator | None = None,
    approval_store: InMemoryApprovalStore | None = None,
    client_tool_store: InMemoryClientToolStore | None = None,
) -> Any:
    """Convenience application for drivers that do not mount native routes."""

    try:
        from fastapi import FastAPI
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError("The neutral runtime requires FastAPI") from exc

    @asynccontextmanager
    async def lifespan(_app: Any) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await runtime_driver.close()

    runtime_driver = driver
    trace_store = None
    if playground_enabled:
        from .playground_trace import (
            PlaygroundTraceRuntimeDriver,
            PlaygroundTraceStore,
        )

        trace_store = PlaygroundTraceStore()
        runtime_driver = PlaygroundTraceRuntimeDriver(driver, trace_store)

    app = FastAPI(title=f"Harnest: {runtime_driver.info.name}", lifespan=lifespan)
    from .playground import create_playground_router
    from .server_limits import install_request_size_limit

    install_request_size_limit(app, max_request_bytes)
    if playground_enabled:
        app.include_router(create_playground_router(trace_store))
    app.include_router(
        create_neutral_router(
            runtime_driver,
            request_timeout=request_timeout,
            max_concurrency=max_concurrency,
            max_request_bytes=max_request_bytes,
            approval_store=approval_store,
            client_tool_store=client_tool_store,
        )
    )
    install_authentication(app, authenticator)
    return app


__all__ = [
    "AgentInfo",
    "InvocationRequest",
    "InvocationResult",
    "MAX_LIVE_FRAMES",
    "MAX_REQUEST_BYTES",
    "NEUTRAL_USER_ID",
    "NoCustomerFacingOutputError",
    "ResponseRequest",
    "RuntimeDriver",
    "RuntimeEvent",
    "SessionConflictError",
    "SessionMessage",
    "SessionRecord",
    "create_neutral_app",
    "create_neutral_router",
    "require_customer_facing_output",
]
