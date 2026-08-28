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
from .server_config import format_byte_size, validate_max_request_bytes


MAX_REQUEST_BYTES = 1024 * 1024
MAX_LIVE_FRAMES = 1024
NEUTRAL_USER_ID = ANONYMOUS_USER_ID

RuntimeEvent = dict[str, Any]


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


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """A backend-independent view of a persisted agent session."""

    id: str
    user_id: str
    state: Mapping[str, Any]
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class InvocationRequest:
    """The complete input passed from a transport to a runtime driver."""

    input: str
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
    return {"id": session.id, "state": dict(session.state)}


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


def _start_approval_run(
    store: InMemoryApprovalStore,
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


async def _sse_approval_run(
    *,
    store: InMemoryApprovalStore,
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
    run = _start_approval_run(store, driver, request, stream=True)
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
        if kind == "error":
            raise value
        if kind == "result":
            return value
        raise RuntimeError("unexpected approval run notification")


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


def _live_frame_error(frame: Any, max_request_bytes: int = MAX_REQUEST_BYTES) -> str | None:
    if (
        not isinstance(frame, dict)
        or not set(frame) <= {"type", "input", "requestId", "metadata"}
        or frame.get("type") != "response.create"
        or not isinstance(frame.get("input"), str)
        or not frame["input"].strip()
    ):
        return "Invalid live input frame"
    if len(frame["input"].encode("utf-8")) > max_request_bytes:
        return f"Input exceeds {format_byte_size(max_request_bytes)}"
    if not isinstance(frame.get("metadata", {}), dict):
        return "metadata must be an object"
    request_id = frame.get("requestId")
    if request_id is not None and not isinstance(request_id, str):
        return "requestId must be a string"
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
    approval_run = _start_approval_run(approval_store, driver, run, stream=True)
    deadline = asyncio.get_running_loop().time() + request_timeout
    try:
        async with semaphore:
            approval_run.activation.set()
            result = await _consume_live_run(
                websocket,
                approval_run,
                state,
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

    def parse_response(payload: Mapping[str, Any]) -> tuple[str, bool, dict[str, Any]]:
        if not set(payload) <= {"input", "sessionId", "stream", "metadata"}:
            raise HTTPException(status_code=400, detail="Invalid response request")
        text = payload.get("input")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(status_code=400, detail="input must be non-empty")
        if len(text.encode("utf-8")) > max_request_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Input exceeds {format_byte_size(max_request_bytes)}",
            )
        stream = payload.get("stream", False)
        if not isinstance(stream, bool):
            raise HTTPException(status_code=400, detail="stream must be boolean")
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, dict):
            raise HTTPException(status_code=400, detail="metadata must be an object")
        return text, stream, metadata

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
                "live": "/live",
                "approvals": "/approvals/{approvalId}",
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

    @router.post("/responses")
    async def responses(request: Request) -> Any:
        payload = await read_json(request)
        text, stream, metadata = parse_response(payload)
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
            approval_run = _start_approval_run(approvals, driver, run, stream=False)
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
                error = _live_frame_error(frame, max_request_bytes)
                if error is not None:
                    await websocket.send_json({"type": "error", "error": error})
                    continue
                await _serve_live_frame(
                    websocket,
                    frame,
                    session,
                    invocation=invocation,
                    semaphore=semaphore,
                    driver=driver,
                    approval_store=approvals,
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
    "RuntimeDriver",
    "RuntimeEvent",
    "SessionConflictError",
    "SessionRecord",
    "create_neutral_app",
    "create_neutral_router",
    "require_customer_facing_output",
]
