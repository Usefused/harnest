"""Shared approval, client-tool, and response continuation primitives."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from contextlib import aclosing, nullcontext
from typing import Any

from .approval import (
    ApprovalExecution,
    ApprovalRun,
    InMemoryApprovalStore,
    PendingApproval,
    approval_execution,
)
from .client_tool import (
    ClientToolExecution,
    InMemoryClientToolStore,
    PendingClientTool,
    client_tool_execution,
)
from .runtime_contract import (
    InvocationRequest,
    InvocationResult,
    RuntimeDriver,
    RuntimeEvent,
)
from .external_continuation import PendingExternalContinuation
from .durable import NativeDurableSuspended


def public_output_item(event: RuntimeEvent) -> dict[str, Any]:
    """Convert one internal event into its stable public output representation."""

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


def public_output(events: Sequence[RuntimeEvent]) -> list[dict[str, Any]]:
    """Render events while preserving the established adjacent-text folding."""

    output: list[dict[str, Any]] = []
    for event in events:
        item = public_output_item(event)
        if item["type"] == "message" and output and output[-1]["type"] == "message":
            output[-1]["content"][0]["text"] += item["content"][0]["text"]
        else:
            output.append(item)
    return output


def completed_payload(
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
    """Build the response-completed wire payload shared by all transports."""

    payload: dict[str, Any] = {
        "type": "response.completed",
        "sequence": sequence,
        "responseId": response_id,
        "sessionId": session_id,
        "status": "completed",
        "outputText": text,
        "output": public_output(events),
        "metadata": dict(metadata),
    }
    if request_id is not None:
        payload["requestId"] = request_id
    if result is not None:
        payload["result"] = result
    return payload


def requires_action_payload(
    pending: PendingApproval,
    *,
    response_id: str,
    session_id: str,
    sequence: int,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build the stable human-approval completion payload."""

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


def client_requires_action_payload(
    pending: PendingClientTool,
    *,
    response_id: str,
    session_id: str,
    sequence: int,
) -> dict[str, Any]:
    """Build the stable client-tool completion payload."""

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


def external_in_progress_payload(
    pending: PendingExternalContinuation,
    *,
    response_id: str,
    session_id: str,
    sequence: int,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build the opaque external-wait payload shared by every transport."""

    payload: dict[str, Any] = {
        "type": "response.in_progress",
        "sequence": sequence,
        "responseId": response_id,
        "sessionId": session_id,
        "status": "in_progress",
        "pendingAction": pending.public(),
        "outputText": "",
        "output": [],
        "metadata": {},
    }
    if request_id is not None:
        payload["requestId"] = request_id
    return payload


def final_event_result(events: Sequence[RuntimeEvent]) -> Any:
    """Read the portable result carried by the final output event."""

    for event in reversed(events):
        event_type = event.get("type")
        if event_type not in {"graph_output", "output"}:
            continue
        if "result" in event:
            return event["result"]
        return (
            event.get("output")
            if event_type == "graph_output"
            else event.get("value")
        )
    return None


async def collect_stream(
    driver: RuntimeDriver, request: InvocationRequest, run: ApprovalRun
) -> InvocationResult:
    """Collect a driver stream while exposing each event to continuation logic."""

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
        result=final_event_result(events),
        session_id=request.session_id,
        metadata=dict(request.metadata),
    )


def start_approval_run(
    store: InMemoryApprovalStore,
    client_tools: InMemoryClientToolStore,
    driver: RuntimeDriver,
    request: InvocationRequest,
    *,
    stream: bool,
    external_continuations: Any | None = None,
) -> ApprovalRun:
    """Start a resumable invocation under every process-local wait authority."""

    run = store.create_run(
        user_id=request.user_id,
        session_id=request.session_id,
        call_id=request.invocation_id,
    )

    async def execute() -> None:
        """Wait for the concurrency gate before entering scoped continuations."""

        await run.activation.wait()
        execution = ClientToolExecution(client_tools, run)
        try:
            # Client tools wrap approvals because a resumed approved tool may
            # itself suspend on a browser- or application-owned client tool.
            continuation_scope = (
                nullcontext()
                if external_continuations is None
                else external_continuations.execution(run, request)
            )
            with continuation_scope, client_tool_execution(execution):
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
                        await collect_stream(driver, request, run)
                        if stream
                        else await driver.invoke(request)
                    )
        except NativeDurableSuspended:
            # The framework adapter raises only after its native checkpoint is
            # committed. Arming here closes the callback-before-checkpoint race
            # without retaining the suspended Python coroutine.
            if external_continuations is None:
                run.notifications.put_nowait(
                    ("error", RuntimeError("durable suspension is unavailable"))
                )
            else:
                try:
                    await external_continuations.arm(
                        response_id=request.invocation_id,
                        user_id=request.user_id,
                        session_id=request.session_id,
                    )
                except BaseException as exc:
                    run.notifications.put_nowait(("error", exc))
        except BaseException as exc:
            run.notifications.put_nowait(("error", exc))
        else:
            run.notifications.put_nowait(("result", result))
        finally:
            # This owns transient bytes from every boundary: request input,
            # server tools, and client tools. Model adapters normally commit
            # consumed leases earlier; terminal cleanup covers every other path.
            execution.transient_media.clear()

    run.task = asyncio.create_task(execute())
    return run


async def next_run_boundary(run: ApprovalRun, *, timeout: float) -> tuple[str, Any]:
    """Wait for the next result, suspension, event, or failure boundary."""

    return await asyncio.wait_for(run.notifications.get(), timeout=timeout)


async def next_non_event(run: ApprovalRun, *, deadline: float) -> tuple[str, Any]:
    """Skip already-streamed events while retaining one absolute timeout."""

    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        kind, value = await next_run_boundary(run, timeout=remaining)
        if kind != "event":
            return kind, value


def json_requires_action(
    pending: PendingApproval, *, response_id: str, session_id: str
) -> dict[str, Any]:
    """Adapt a human-approval completion to the non-streaming JSON contract."""

    required = requires_action_payload(
        pending,
        response_id=response_id,
        session_id=session_id,
        sequence=0,
    )
    required.pop("type")
    required.pop("sequence")
    required["id"] = required.pop("responseId")
    return required


def json_client_requires_action(
    pending: PendingClientTool, *, response_id: str, session_id: str
) -> dict[str, Any]:
    """Adapt a client-tool completion to the non-streaming JSON contract."""

    required = client_requires_action_payload(
        pending,
        response_id=response_id,
        session_id=session_id,
        sequence=0,
    )
    required.pop("type")
    required.pop("sequence")
    required["id"] = required.pop("responseId")
    return required
