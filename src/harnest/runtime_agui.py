"""AG-UI protocol bridge for the framework-neutral Harnest runtime.

AG-UI (https://ag-ui.com) is an open event-streaming protocol for connecting
agents to front-end applications: a client POSTs a `RunAgentInput` and the
agent replies with a `text/event-stream` of typed lifecycle, message,
tool-call, and state events. This module is a thin transport adapter, not a
second execution path: it uses `InvocationCoordinator.stream_response`,
just like `/responses`, and translates the common response events into
AG-UI wire shapes. The encoder owns no execution or continuation state.

Scope: this bridge covers AG-UI's core run lifecycle (`RUN_STARTED`, text
messages, tool calls, `STATE_DELTA`, `RUN_FINISHED`/`RUN_ERROR`). This adapter
does not yet implement AG-UI interruption/resume handling.
A mid-run approval or client-tool suspension reports `RUN_ERROR`; use
`/responses` or `/live` for those continuations. The common response pipeline
retains their normal status and authorization boundaries.

`STATE_DELTA` is currently only emitted for LangGraph applications, which
merge an authored `Event.state_delta` into a Harnest-owned `_harnest_state`
channel that nothing else writes to. ADK reuses its native
`EventActions.state_delta` for internal framework bookkeeping as well as
authored state, so it cannot be forwarded without risking a privacy leak;
surfacing it safely would need session-state diffing instead, which is not
implemented yet.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Mapping

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from .runtime_auth import principal_for
from .runtime_invocation import InvocationCoordinator


def mount_agui_routes(router: Any, *, coordinator: InvocationCoordinator) -> None:
    """Mount the single AG-UI run endpoint onto the shared neutral router."""

    from .neutral_runtime import _read_request_body

    @router.post("/agui", include_in_schema=False)
    async def agui_run(request: Request) -> Any:
        """Start one AG-UI run and stream its events as they are produced."""

        body = await _read_request_body(request, coordinator.max_request_bytes)
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid UTF-8 JSON") from exc
        if not isinstance(payload, Mapping):
            raise HTTPException(status_code=400, detail="Expected a JSON object")
        thread_id, run_id, text = _parse_run_agent_input(payload)
        user_id = principal_for(request).user_id
        run_request = await coordinator.prepare_request(
            text,
            user_id=user_id,
            session_id=thread_id,
            metadata={},
            transport="agui",
        )
        encoder = _AGUIEncoder(run_id=run_id)
        return StreamingResponse(
            coordinator.stream_response(run_request, encoder=encoder.encode),
            media_type="text/event-stream",
        )


def _parse_run_agent_input(payload: Mapping[str, Any]) -> tuple[str | None, str, str]:
    """Extract the thread, run, and latest user text from a `RunAgentInput` body."""

    thread_id, run_id = _parse_run_identity(payload)
    return thread_id, run_id, _latest_user_text(payload)


def _parse_run_identity(payload: Mapping[str, Any]) -> tuple[str | None, str]:
    """Validate the optional `threadId` and default an absent `runId`."""

    thread_id = payload.get("threadId")
    if thread_id is not None and not isinstance(thread_id, str):
        raise HTTPException(status_code=400, detail="threadId must be a string")
    run_id = payload.get("runId")
    run_id = run_id if isinstance(run_id, str) and run_id else uuid.uuid4().hex
    return thread_id, run_id


def _latest_user_text(payload: Mapping[str, Any]) -> str:
    """Return the newest non-empty user message, the single-turn `input`.

    AG-UI resends the full thread on every run; only the newest user turn is
    a new invocation, matching the neutral runtime's single-turn `input`.
    """

    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="messages must be a list")
    text = next(
        (
            message.get("content")
            for message in reversed(messages)
            if isinstance(message, Mapping) and message.get("role") == "user"
        ),
        None,
    )
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(
            status_code=400, detail="messages must include non-empty user content"
        )
    return text


class _AGUIEncoder:
    """Track open text messages and tool calls to emit well-formed AG-UI frames."""

    def __init__(self, *, run_id: str) -> None:
        """Keep only AG-UI framing state; the coordinator owns the run."""

        self._run_id = run_id
        self._open_message: str | None = None
        self._message_agent: str | None = None
        self._message_count = 0

    def encode(self, name: str, payload: Mapping[str, Any]) -> str:
        """Encode shared response events without executing or managing a run."""

        if name == "response.created":
            events = [
                {
                    "type": "RUN_STARTED",
                    "threadId": payload["sessionId"],
                    "runId": self._run_id,
                }
            ]
        elif name == "error":
            events = self.close() + [{"type": "RUN_ERROR", "message": payload["error"]}]
        elif name in {"response.completed", "response.in_progress"}:
            events = self._terminal_events(payload)
        elif name.startswith("response."):
            events = self.translate(_neutral_event(name, payload))
        else:
            # Pending-action announcements precede the shared terminal receipt;
            # report the adapter's unsupported continuation there, exactly once.
            events = []
        return "".join(_agui_sse(event) for event in events)

    def _terminal_events(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Map shared completion and suspension receipts to AG-UI boundaries."""

        if payload.get("status") != "completed":
            return self.close() + [
                {
                    "type": "RUN_ERROR",
                    "message": "This AG-UI adapter does not yet support approval, client-tool, or external continuations; use /responses or /live",
                }
            ]
        return self.close() + [
            {
                "type": "RUN_FINISHED",
                "threadId": payload["sessionId"],
                "runId": self._run_id,
            }
        ]

    def translate(self, event: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Convert one neutral `RuntimeEvent` into zero or more AG-UI events."""

        event_type = event.get("type")
        if event_type == "message":
            return self._message_delta(event)
        # Every other event type interrupts a streamed text message, so close
        # it first rather than leaving an unterminated AG-UI message frame.
        boundary = self.close()
        if event_type == "tool_call":
            return boundary + self._tool_call(event)
        if event_type == "tool_result":
            return boundary + self._tool_result(event)
        if event_type == "state_delta":
            return boundary + self._state_delta(event)
        if event_type in {
            "agent_activity",
            "agent_metadata",
            "decision_result",
            "thinking",
        }:
            # AG-UI's CUSTOM event is the documented escape hatch for
            # application-specific signals outside the core vocabulary.
            return boundary + [
                {"type": "CUSTOM", "name": event_type, "value": dict(event)}
            ]
        # Terminal graph/output events are carried by RUN_FINISHED, not a
        # discrete frame, matching the neutral SSE and A2A transports.
        return boundary

    def _message_delta(self, event: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Open, continue, or restart a `TEXT_MESSAGE_*` sequence for one agent."""

        agent = event.get("agent") if isinstance(event.get("agent"), str) else None
        events: list[dict[str, Any]] = []
        if self._open_message is not None and agent != self._message_agent:
            # A different attributed agent cannot continue the same AG-UI
            # message; close it before opening a fresh one.
            events.extend(self.close())
        if self._open_message is None:
            self._message_count += 1
            self._open_message = f"msg-{self._message_count}"
            self._message_agent = agent
            events.append(
                {
                    "type": "TEXT_MESSAGE_START",
                    "messageId": self._open_message,
                    "role": "assistant",
                }
            )
        delta = event.get("text", "")
        if delta:
            events.append(
                {
                    "type": "TEXT_MESSAGE_CONTENT",
                    "messageId": self._open_message,
                    "delta": delta,
                }
            )
        return events

    def _tool_call(self, event: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Emit one atomic start/args/end triple for a completed tool call.

        Harnest's neutral `tool_call` event already carries the full call
        rather than incremental argument fragments, so the three AG-UI frames
        are emitted back to back instead of interleaved with deltas.
        """

        call_id = event.get("id") or uuid.uuid4().hex
        return [
            {
                "type": "TOOL_CALL_START",
                "toolCallId": call_id,
                "toolCallName": event.get("name"),
            },
            {
                "type": "TOOL_CALL_ARGS",
                "toolCallId": call_id,
                "delta": json.dumps(event.get("arguments") or {}),
            },
            {"type": "TOOL_CALL_END", "toolCallId": call_id},
        ]

    def _tool_result(self, event: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Emit the result AG-UI attaches to its originating tool call."""

        call_id = event.get("id", event.get("callId")) or uuid.uuid4().hex
        content = event.get("result", event.get("output"))
        return [
            {
                "type": "TOOL_CALL_RESULT",
                "messageId": uuid.uuid4().hex,
                "toolCallId": call_id,
                "content": content if isinstance(content, str) else json.dumps(content),
            }
        ]

    def _state_delta(self, event: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Render one shallow state patch as an RFC 6902 `STATE_DELTA` event."""

        delta = event.get("delta")
        if not isinstance(delta, Mapping) or not delta:
            return []
        # RFC 6902 "add" replaces an existing object member in place, so a
        # flat "add" per key is correct whether or not the key already
        # existed in the client's state, without tracking prior state here.
        patch = [
            {"op": "add", "path": f"/{_json_pointer_escape(key)}", "value": value}
            for key, value in delta.items()
        ]
        return [{"type": "STATE_DELTA", "delta": patch}]

    def close(self) -> list[dict[str, Any]]:
        """End the currently open text message, if any."""

        if self._open_message is None:
            return []
        event = {"type": "TEXT_MESSAGE_END", "messageId": self._open_message}
        self._open_message = None
        self._message_agent = None
        return [event]


def _neutral_event(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt normalized response fields for the protocol's event encoder."""

    event = {**payload, "type": name.removeprefix("response.")}
    if name in {"response.text.delta", "response.thinking.delta"}:
        event["type"] = "message" if name == "response.text.delta" else "thinking"
        event["text"] = payload.get("delta", "")
    return event


def _json_pointer_escape(key: str) -> str:
    """Escape one state key for use as an RFC 6901 JSON Pointer segment."""

    return key.replace("~", "~0").replace("/", "~1")


def _agui_sse(event: Mapping[str, Any]) -> str:
    """Encode one AG-UI event using its plain, unnamed SSE data frame."""

    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


__all__ = ["mount_agui_routes"]
