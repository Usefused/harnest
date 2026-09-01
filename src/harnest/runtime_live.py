"""WebSocket framing and continuation handling for the neutral live API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from typing import Any, Mapping
import uuid

from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.websockets import WebSocketDisconnect

from .approval import ApprovalRun, InMemoryApprovalStore
from .client_tool import (
    ClientToolError,
    InMemoryClientToolStore,
    PendingClientTool,
)
from .logging import get_logger
from .runtime_continuation import (
    completed_payload,
    external_in_progress_payload,
    next_run_boundary,
    requires_action_payload,
    start_approval_run,
)
from .runtime_contract import (
    InvocationResult,
    RuntimeDriver,
    RuntimeEvent,
    SessionRecord,
    require_customer_facing_output,
)
from .runtime_sse import approval_payload, client_tool_payload, stream_frame
from .server_config import format_byte_size


_AUDIT = get_logger("live.audit")


class LiveResponseCancelled(RuntimeError):
    """Stop one active live response without closing its caller-owned socket."""


class LiveProtocolError(RuntimeError):
    """Reject a frame that cannot apply to the active live response."""


@dataclass(slots=True)
class LiveStreamState:
    """Retain sequence progress so mid-stream errors keep the wire contract."""

    events: list[RuntimeEvent] = field(default_factory=list)
    sequence: int = 1
    pending_client_tool: PendingClientTool | None = field(default=None, repr=False)
    client_tool_result_received: bool = field(default=False, repr=False)


async def _consume_live_run(
    websocket: Any,
    run: ApprovalRun,
    state: LiveStreamState,
    *,
    inbound_frames: asyncio.Queue[Any],
    client_tools: InMemoryClientToolStore,
    deadline: float,
    response_id: str,
    session_id: str,
    request_id: str | None,
) -> InvocationResult | None:
    """Forward run boundaries until the live response completes or suspends."""

    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        kind, value = await next_run_boundary(run, timeout=remaining)
        if kind == "event":
            state.events.append(value)
            normalized = stream_frame(
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
        if kind in {"approval", "external_continuation"}:
            await _send_live_suspension(
                websocket,
                kind,
                value,
                response_id=response_id,
                session_id=session_id,
                sequence=state.sequence,
                request_id=request_id,
            )
            return None
        if kind == "client_tool":
            state.pending_client_tool = value
            state.client_tool_result_received = False
            try:
                await websocket.send_json(
                    client_tool_payload(
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
                output = await asyncio.wait_for(
                    inbound_frames.get(), timeout=remaining
                )
                await client_tools.submit(
                    value.id, user_id=value.user_id, output=output
                )
            finally:
                state.pending_client_tool = None
                state.client_tool_result_received = False
            state.sequence += 1
            continue
        if kind == "error":
            raise value
        if kind == "result":
            return value
        raise RuntimeError("unexpected approval run notification")


async def _send_live_suspension(
    websocket: Any,
    kind: str,
    value: Any,
    *,
    response_id: str,
    session_id: str,
    sequence: int,
    request_id: str | None,
) -> None:
    """Render terminal live boundaries while keeping the consume loop bounded."""

    if kind == "external_continuation":
        await websocket.send_json(
            external_in_progress_payload(
                value,
                response_id=response_id,
                session_id=session_id,
                sequence=sequence,
                request_id=request_id,
            )
        )
        return
    await websocket.send_json(
        approval_payload(
            value,
            response_id=response_id,
            session_id=session_id,
            sequence=sequence,
            request_id=request_id,
        )
    )
    await websocket.send_json(
        requires_action_payload(
            value,
            response_id=response_id,
            session_id=session_id,
            sequence=sequence + 1,
            request_id=request_id,
        )
    )


def _live_client_tool_result(frame: Any, request_id: str) -> Any:
    """Validate that a browser result answers the currently pending tool."""

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


def _cancelled_payload(
    *,
    response_id: str,
    session_id: str,
    sequence: int,
    metadata: Mapping[str, Any],
    request_id: str | None,
) -> dict[str, Any]:
    """Return a terminal cancellation without claiming partial text was committed."""

    payload: dict[str, Any] = {
        "type": "response.completed",
        "sequence": sequence,
        "responseId": response_id,
        "sessionId": session_id,
        "status": "cancelled",
        "outputText": "",
        "output": [],
        "metadata": dict(metadata),
    }
    if request_id is not None:
        payload["requestId"] = request_id
    return payload


def _route_active_frame(
    frame: Any,
    *,
    response_id: str,
    state: LiveStreamState,
    inbound_frames: asyncio.Queue[Any],
) -> None:
    """Route one active-response frame without allowing stale cancellation."""

    if isinstance(frame, dict) and frame.get("type") == "response.cancel":
        if (
            set(frame) != {"type", "responseId"}
            or frame.get("responseId") != response_id
        ):
            _audit_live_cancel("failed")
            raise LiveProtocolError("Invalid response.cancel frame")
        raise LiveResponseCancelled
    pending = state.pending_client_tool
    if pending is None:
        raise LiveProtocolError(
            "Only response.cancel is accepted while a live response is active"
        )
    if state.client_tool_result_received:
        raise ClientToolError("client tool result was already submitted")
    output = _live_client_tool_result(frame, pending.id)
    # Reserve the single inbound slot before the consumer resumes so duplicate
    # submissions cannot race validation and attach to a later client tool.
    state.client_tool_result_received = True
    inbound_frames.put_nowait(output)


async def _settle_cancelled_task(task: asyncio.Task[Any]) -> None:
    """Cancel and consume one transport-owned task without hiding its cleanup."""

    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _drive_live_response(
    websocket: Any,
    execution: asyncio.Task[None],
    *,
    response_id: str,
    state: LiveStreamState,
    inbound_frames: asyncio.Queue[Any],
) -> None:
    """Keep receiving control frames while one response owns the send stream."""

    receive = asyncio.create_task(websocket.receive_json())
    try:
        while True:
            done, _ = await asyncio.wait(
                {execution, receive}, return_when=asyncio.FIRST_COMPLETED
            )
            if execution in done:
                await execution
                return
            frame = receive.result()
            _route_active_frame(
                frame,
                response_id=response_id,
                state=state,
                inbound_frames=inbound_frames,
            )
            receive = asyncio.create_task(websocket.receive_json())
    finally:
        await _settle_cancelled_task(receive)


async def _cancel_live_execution(
    store: InMemoryApprovalStore,
    run: ApprovalRun,
    execution: asyncio.Task[None],
) -> None:
    """Cancel both transport and framework tasks before acknowledging the user."""

    store.cancel_run(run)
    await _settle_cancelled_task(execution)
    if run.task is not None:
        await _settle_cancelled_task(run.task)


def _audit_live_cancel(outcome: str) -> None:
    """Record user cancellation without response, session, prompt, or output data."""

    _AUDIT.info(
        "live.response_cancel",
        operation="response.cancel",
        trigger="user",
        outcome=outcome,
        action="live_response",
    )


async def live_session(websocket: Any, response_session: Any) -> SessionRecord | None:
    """Authorize the first live frame and establish its caller-owned session."""

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


def validated_live_frame(
    frame: Any,
    max_request_bytes: int,
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


async def _execute_live_response(
    websocket: Any,
    run: ApprovalRun,
    state: LiveStreamState,
    *,
    inbound_frames: asyncio.Queue[Any],
    semaphore: asyncio.Semaphore,
    client_tools: InMemoryClientToolStore,
    deadline: float,
    response_id: str,
    session_id: str,
    request_id: str | None,
    metadata: Mapping[str, Any],
) -> None:
    """Run and publish one response while the caller independently reads controls."""

    async with semaphore:
        run.activation.set()
        result = await _consume_live_run(
            websocket,
            run,
            state,
            inbound_frames=inbound_frames,
            client_tools=client_tools,
            deadline=deadline,
            response_id=response_id,
            session_id=session_id,
            request_id=request_id,
        )
        if result is None:
            return
    require_customer_facing_output(result.text, result.result)
    await websocket.send_json(
        completed_payload(
            response_id=response_id,
            session_id=session_id,
            sequence=state.sequence,
            events=state.events,
            text=result.text,
            metadata=metadata,
            result=result.result,
            request_id=request_id,
        )
    )


async def serve_live_frame(
    websocket: Any,
    frame: Mapping[str, Any],
    session: SessionRecord,
    *,
    invocation: Any,
    semaphore: asyncio.Semaphore,
    driver: RuntimeDriver,
    approval_store: InMemoryApprovalStore,
    client_tool_store: InMemoryClientToolStore,
    external_continuations: Any | None = None,
    request_timeout: float,
) -> None:
    """Execute one validated live request and preserve continuation semantics."""

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
    state = LiveStreamState()
    approval_run = start_approval_run(
        approval_store,
        client_tool_store,
        driver,
        run,
        stream=True,
        external_continuations=external_continuations,
    )
    deadline = asyncio.get_running_loop().time() + request_timeout
    inbound_frames: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)
    execution = asyncio.create_task(
        _execute_live_response(
            websocket,
            approval_run,
            state,
            inbound_frames=inbound_frames,
            semaphore=semaphore,
            client_tools=client_tool_store,
            deadline=deadline,
            response_id=response_id,
            session_id=session.id,
            request_id=request_id,
            metadata=metadata,
        )
    )
    try:
        await _drive_live_response(
            websocket,
            execution,
            response_id=response_id,
            state=state,
            inbound_frames=inbound_frames,
        )
    except LiveResponseCancelled:
        await _cancel_live_execution(approval_store, approval_run, execution)
        _audit_live_cancel("committed")
        await websocket.send_json(
            _cancelled_payload(
                response_id=response_id,
                session_id=session.id,
                sequence=state.sequence,
                metadata=metadata,
                request_id=request_id,
            )
        )
    except asyncio.CancelledError:
        await _cancel_live_execution(approval_store, approval_run, execution)
        raise
    except WebSocketDisconnect:
        await _cancel_live_execution(approval_store, approval_run, execution)
        raise
    except Exception as exc:
        # A terminal protocol or execution error cannot leave model/tool work
        # detached from the socket that owns its response.
        await _cancel_live_execution(approval_store, approval_run, execution)
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


__all__ = ["live_session", "serve_live_frame", "validated_live_frame"]
