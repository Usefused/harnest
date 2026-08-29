"""WebSocket framing and continuation handling for the neutral live API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from typing import Any, Mapping
import uuid

from pydantic import ValidationError
from starlette.exceptions import HTTPException

from .approval import ApprovalRun, InMemoryApprovalStore
from .client_tool import ClientToolError, InMemoryClientToolStore
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


@dataclass(slots=True)
class LiveStreamState:
    """Retain sequence progress so mid-stream errors keep the wire contract."""

    events: list[RuntimeEvent] = field(default_factory=list)
    sequence: int = 1


async def _consume_live_run(
    websocket: Any,
    run: ApprovalRun,
    state: LiveStreamState,
    *,
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
            frame = await asyncio.wait_for(
                websocket.receive_json(), timeout=remaining
            )
            output = _live_client_tool_result(frame, value.id)
            await client_tools.submit(
                value.id, user_id=value.user_id, output=output
            )
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
        require_customer_facing_output(result.text, result.result)
        await websocket.send_json(
            completed_payload(
                response_id=response_id,
                session_id=session.id,
                sequence=state.sequence,
                events=state.events,
                text=result.text,
                metadata=metadata,
                result=result.result,
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


__all__ = ["live_session", "serve_live_frame", "validated_live_frame"]
