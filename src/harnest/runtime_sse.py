"""SSE framing and approval continuation adapters for the neutral runtime."""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Mapping

from starlette.exceptions import HTTPException

from .approval import (
    ApprovalEnforcementError,
    ApprovalRun,
    InMemoryApprovalStore,
    PendingApproval,
)
from .client_tool import InMemoryClientToolStore, PendingClientTool
from .runtime_continuation import (
    _public_activity_fields,
    _public_event_agent,
    client_requires_action_payload,
    completed_payload,
    external_in_progress_payload,
    json_client_requires_action,
    json_requires_action,
    next_non_event,
    next_run_boundary,
    requires_action_payload,
    start_approval_run,
)
from .runtime_contract import (
    InvocationRequest,
    RuntimeDriver,
    RuntimeEvent,
    require_customer_facing_output,
)
from .output import _agent_metadata_from_runtime_event
from .response_status import InMemoryResponseStatusStore


def stream_frame(
    event: RuntimeEvent,
    *,
    sequence: int,
    response_id: str,
    session_id: str,
    request_id: str | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Translate one portable runtime event into an incremental wire frame."""

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
            **_public_event_agent(event),
            "type": "response.text.delta",
            "delta": event.get("text", ""),
        }
    if event_type == "thinking":
        return "response.thinking.delta", {
            **common,
            **_public_event_agent(event),
            "type": "response.thinking.delta",
            "delta": event.get("text", ""),
        }
    if event_type == "agent_activity":
        return "response.agent_activity", {
            **common,
            "type": "response.agent_activity",
            **_public_activity_fields(event),
        }
    if event_type == "agent_metadata":
        return "response.agent_metadata", {
            **common,
            **_public_event_agent(event),
            "type": "response.agent_metadata",
            **_agent_metadata_from_runtime_event(event).as_dict(),
        }
    if event_type == "tool_call":
        return "response.tool_call", {
            **common,
            **_public_event_agent(event),
            "type": "response.tool_call",
            "id": event.get("id"),
            "name": event.get("name"),
            "arguments": event.get("arguments"),
        }
    if event_type == "tool_result":
        return "response.tool_result", {
            **common,
            **_public_event_agent(event),
            "type": "response.tool_result",
            "callId": event.get("id", event.get("callId")),
            "name": event.get("name"),
            "output": event.get("result", event.get("output")),
        }
    # Terminal graph/output events belong in the completed response rather
    # than a transport-specific incremental frame.
    if event_type in {"graph_output", "output"}:
        return None
    raise ValueError(f"unsupported runtime event type: {event_type!r}")


def approval_payload(
    pending: PendingApproval,
    *,
    response_id: str,
    session_id: str,
    sequence: int,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build the transport event announcing a pending human approval."""

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


def client_tool_payload(
    pending: PendingClientTool,
    *,
    response_id: str,
    session_id: str,
    sequence: int,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build the transport event announcing a pending client tool."""

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


def resumed_action_payload(
    kind: str,
    value: Any,
    *,
    response_id: str,
    session_id: str,
    client_tool_id: str,
) -> dict[str, Any]:
    """Map a resumed continuation boundary onto the established JSON shape."""

    if kind == "approval":
        payload = json_requires_action(
            value, response_id=response_id, session_id=session_id
        )
        payload["approvalId"] = value.id
        return payload
    if kind == "client_tool":
        return json_client_requires_action(
            value, response_id=response_id, session_id=session_id
        )
    if kind == "error":
        raise value
    if kind != "result":
        raise RuntimeError("unexpected runtime action notification")
    require_customer_facing_output(value.text, value.result)
    payload = completed_payload(
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


async def sse_approval_run(
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
    response_statuses: InMemoryResponseStatusStore | None = None,
    external_continuations: Any | None = None,
) -> AsyncIterator[str]:
    """Stream one invocation through shared approval and client-tool state."""

    sequence = 0
    yield sse(
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
    run = start_approval_run(
        store,
        client_tools,
        driver,
        request,
        stream=True,
        external_continuations=external_continuations,
    )
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
                user_id=request.user_id,
                response_statuses=response_statuses,
            ):
                yield frame
    except asyncio.CancelledError:
        store.cancel_run(run)
        _record_response_status(
            response_statuses,
            {
                "type": "response.completed",
                "sequence": sequence,
                "responseId": response_id,
                "sessionId": session_id,
                "status": "cancelled",
                "outputText": "",
                "output": [],
                "metadata": dict(metadata),
            },
            response_id=response_id,
            user_id=request.user_id,
            session_id=session_id,
        )
        raise
    except Exception as exc:
        if isinstance(exc, asyncio.TimeoutError):
            store.cancel_run(run)
        failed = {
            "type": "response.completed",
            "sequence": sequence,
            "responseId": response_id,
            "sessionId": session_id,
            "status": "failed",
            "error": {"code": "invocation_failed"},
            "outputText": "",
            "output": [],
            "metadata": dict(metadata),
        }
        _record_response_status(
            response_statuses,
            failed,
            response_id=response_id,
            user_id=request.user_id,
            session_id=session_id,
        )
        yield sse(
            "error",
            {
                "type": "error",
                "sequence": sequence,
                "responseId": response_id,
                "sessionId": session_id,
                "error": (
                    "Response timed out"
                    if isinstance(exc, asyncio.TimeoutError)
                    else str(exc)
                ),
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
    user_id: str,
    response_statuses: InMemoryResponseStatusStore | None,
) -> AsyncIterator[str]:
    """Yield incremental frames until the run completes or suspends."""

    sequence = start_sequence
    events: list[RuntimeEvent] = []
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        kind, value = await next_run_boundary(run, timeout=remaining)
        if kind == "event":
            events.append(value)
            frame = stream_frame(
                value,
                sequence=sequence,
                response_id=response_id,
                session_id=session_id,
            )
            if frame is not None:
                yield sse(*frame)
                sequence += 1
            continue
        if kind == "approval":
            status = requires_action_payload(
                value,
                response_id=response_id,
                session_id=session_id,
                sequence=sequence + 1,
            )
            _record_response_status(
                response_statuses,
                status,
                response_id=response_id,
                user_id=user_id,
                session_id=session_id,
            )
            yield sse(
                "approval.requested",
                approval_payload(
                    value,
                    response_id=response_id,
                    session_id=session_id,
                    sequence=sequence,
                ),
            )
            yield sse(
                "response.completed",
                status,
            )
            return
        if kind == "client_tool":
            status = client_requires_action_payload(
                value,
                response_id=response_id,
                session_id=session_id,
                sequence=sequence + 1,
            )
            _record_response_status(
                response_statuses,
                status,
                response_id=response_id,
                user_id=user_id,
                session_id=session_id,
            )
            yield sse(
                "client_tool.requested",
                client_tool_payload(
                    value,
                    response_id=response_id,
                    session_id=session_id,
                    sequence=sequence,
                ),
            )
            yield sse(
                "response.completed",
                status,
            )
            return
        if kind == "external_continuation":
            status = external_in_progress_payload(
                value,
                response_id=response_id,
                session_id=session_id,
                sequence=sequence,
            )
            _record_response_status(
                response_statuses,
                status,
                response_id=response_id,
                user_id=user_id,
                session_id=session_id,
            )
            yield sse(
                "response.in_progress",
                status,
            )
            return
        if kind == "error":
            raise value
        if kind != "result":
            raise RuntimeError("unexpected approval run notification")
        require_customer_facing_output(value.text, value.result)
        status = completed_payload(
            response_id=response_id,
            session_id=session_id,
            sequence=sequence,
            events=events,
            text=value.text,
            metadata=metadata,
            result=value.result,
        )
        _record_response_status(
            response_statuses,
            status,
            response_id=response_id,
            user_id=user_id,
            session_id=session_id,
        )
        yield sse(
            "response.completed",
            status,
        )
        return


def _record_response_status(
    store: InMemoryResponseStatusStore | None,
    payload: Mapping[str, Any],
    *,
    response_id: str,
    user_id: str,
    session_id: str,
) -> None:
    """Publish a transport result only when the host exposes status polling."""

    if store is not None:
        store.record(
            response_id=response_id,
            user_id=user_id,
            session_id=session_id,
            payload=payload,
        )


async def resume_approval_run(
    store: InMemoryApprovalStore,
    pending: PendingApproval,
    run: ApprovalRun,
    *,
    semaphore: asyncio.Semaphore,
    request_timeout: float,
) -> tuple[str, Any]:
    """Deliver approval and await the next non-streaming continuation boundary."""

    async with semaphore:
        store.deliver_decision(pending, "approve")
        deadline = asyncio.get_running_loop().time() + request_timeout
        return await next_non_event(run, deadline=deadline)


def parse_approval_decision(payload: Mapping[str, Any]) -> str:
    """Validate the intentionally narrow approval decision request."""

    if set(payload) != {"decision"}:
        raise HTTPException(status_code=400, detail="Expected approval decision")
    decision = payload.get("decision")
    if decision not in {"approve", "deny"}:
        raise HTTPException(status_code=400, detail="decision must be approve or deny")
    return decision


def commit_approval_decision(
    store: InMemoryApprovalStore,
    approval_id: str,
    *,
    user_id: str,
    decision: str,
) -> PendingApproval:
    """Atomically record one caller-scoped approval decision."""

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


def sse(event_name: str, payload: Mapping[str, Any]) -> str:
    """Encode one named event using the neutral SSE wire format."""

    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


__all__ = [
    "approval_payload",
    "client_tool_payload",
    "commit_approval_decision",
    "parse_approval_decision",
    "resume_approval_run",
    "resumed_action_payload",
    "sse",
    "sse_approval_run",
    "stream_frame",
]
