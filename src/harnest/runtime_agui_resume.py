"""Adapt AG-UI decisions to Harnest's existing one-time continuation stores."""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException

from .agent.approval import PendingApproval
from .client_tool import PendingClientTool
from .runtime_agui_input import AGUIInput
from .runtime_invocation import InvocationCoordinator


def pending_actions(
    coordinator: InvocationCoordinator, *, user_id: str, session_id: str
) -> list[PendingApproval | PendingClientTool]:
    """Use the shared stores as the only authority for pending interaction."""

    return [
        *coordinator.approvals.pending_for(user_id=user_id, session_id=session_id),
        *coordinator.client_tools.pending_for(user_id=user_id, session_id=session_id),
    ]


def prepare_resume(
    coordinator: InvocationCoordinator, envelope: AGUIInput, *, user_id: str
) -> tuple[Any, Any] | None:
    """Validate correlation before any decision or browser result is delivered."""

    pending = pending_actions(coordinator, user_id=user_id, session_id=envelope.thread_id or "")
    _validate_private_resume(envelope, pending)
    replies = _replies(envelope, pending)
    if not pending:
        if replies:
            raise HTTPException(409, "No matching pending AG-UI interaction")
        return None
    if set(replies) != {item.id for item in pending}:
        raise HTTPException(409, "Resolve every pending interaction before sending new input")
    deliveries = [_delivery(coordinator, item, replies[item.id]) for item in pending]
    run = _shared_run(coordinator, pending)

    async def deliver() -> None:
        """Recheck one-time authority inside the shared execution capacity gate."""

        delivered = False
        try:
            for operation in deliveries:
                await operation()
                delivered = True
        except BaseException:
            # A partially accepted batch cannot be left executing without a
            # stream consumer. A rejected first submission owns no work.
            if delivered:
                coordinator.approvals.cancel_run(run)
                coordinator.record_cancelled_response(request)
            raise

    request = coordinator.create_request(
        "", user_id=user_id, session_id=run.session_id,
        invocation_id=run.call_id, metadata={}, transport="agui",
    )
    return request, (run, deliver)


def _validate_private_resume(envelope: AGUIInput, pending: list[Any]) -> None:
    """Reject transcript-bearing submissions before the encoder can echo them."""

    if any(isinstance(item, PendingClientTool) and item.private_input for item in pending):
        # The encoder echoes messages and state. Private results must travel
        # only in explicit resume payloads, never in the client transcript.
        if envelope.messages or envelope.state:
            raise HTTPException(400, "Private client input resumes must omit messages and state")


def _shared_run(coordinator: InvocationCoordinator, pending: list[Any]) -> Any:
    """Reject attempts to combine decisions belonging to separate executions."""

    runs = [item.run if isinstance(item, PendingClientTool) else coordinator.approvals.run_for(item) for item in pending]
    run = runs[0]
    if run is None or any(other is not run for other in runs):
        raise HTTPException(409, "Interactions must belong to one live invocation")
    return run


def _replies(envelope: AGUIInput, pending: list[Any]) -> dict[str, dict[str, Any]]:
    """Accept protocol interrupts or the ordinary AG-UI tool-result message."""

    replies: dict[str, dict[str, Any]] = {}
    for reply in envelope.resume:
        identity = reply.get("interruptId")
        if not isinstance(identity, str) or identity in replies:
            raise HTTPException(400, "resume requires unique interruptId strings")
        if reply.get("status") not in {"resolved", "cancelled"}:
            raise HTTPException(400, "resume status must be resolved or cancelled")
        replies[identity] = reply
    return replies or _tool_replies(envelope, pending)


def _tool_replies(envelope: AGUIInput, pending: list[Any]) -> dict[str, dict[str, Any]]:
    """Match only newly submitted tool results to outstanding scoped requests."""

    replies: dict[str, dict[str, Any]] = {}
    # Only a trailing tool-result group belongs to this submission. Older tool
    # results in a client's full transcript must never be replayed as new work.
    for message in reversed(envelope.messages):
        if message.get("role") != "tool":
            break
        matches = [item for item in pending if isinstance(item, PendingClientTool)
                   and message.get("toolCallId") == item.agui_tool_call_id]
        if len(matches) != 1:
            raise HTTPException(409, "No matching pending client tool")
        item = matches[0]
        if item.id in replies:
            raise HTTPException(400, "Duplicate tool result")
        replies[item.id] = {"status": "resolved", "payload": _tool_output(message)}
    return replies


def _tool_output(message: dict[str, Any]) -> Any:
    """Decode JSON tool outputs while preserving ordinary string results."""

    content = message.get("content")
    if not isinstance(content, str):
        raise HTTPException(400, "Tool result content must be a string")
    try:
        return json.loads(content)
    except ValueError:
        return content


def _delivery(coordinator: InvocationCoordinator, pending: Any, reply: dict[str, Any]) -> Any:
    """Validate the complete response before constructing a mutation callback."""

    if isinstance(pending, PendingApproval):
        decision = _approval_decision(reply)

        async def approve() -> None:
            """Commit and deliver through the existing audited approval store."""

            coordinator.approvals.decide(pending.id, user_id=pending.user_id, decision=decision)

        return approve
    if reply["status"] == "cancelled":
        if "payload" in reply:
            raise HTTPException(400, "Cancelled resumes must omit payload")

        async def cancel() -> None:
            """Cancel only the scoped client request through its audited store."""

            coordinator.client_tools.cancel(pending.id, user_id=pending.user_id)

        return cancel
    if "payload" not in reply:
        raise HTTPException(400, "Client tool resumes require a payload")

    async def submit() -> None:
        """Preserve output validation, media staging, and refreshed credentials."""

        await coordinator.client_tools.submit(pending.id, user_id=pending.user_id, output=reply["payload"])

    return submit


def _approval_decision(reply: dict[str, Any]) -> str:
    """Keep approvals boolean-only; argument edits need a new authorization."""

    if reply["status"] == "cancelled":
        if "payload" in reply:
            raise HTTPException(400, "Cancelled resumes must omit payload")
        return "deny"
    payload = reply.get("payload")
    if not isinstance(payload, dict) or set(payload) != {"approved"}:
        raise HTTPException(400, "Approval payload must contain only approved")
    if not isinstance(payload["approved"], bool):
        raise HTTPException(400, "approved must be a boolean")
    return "approve" if payload["approved"] else "deny"
