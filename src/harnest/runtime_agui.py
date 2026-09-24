"""AG-UI protocol bridge for the framework-neutral Harnest runtime.

AG-UI (https://ag-ui.com) is an open event-streaming protocol for connecting
agents to front-end applications: a client POSTs a `RunAgentInput` and the
agent replies with a `text/event-stream` of typed lifecycle, message,
tool-call, and state events. This module is a thin transport adapter, not a
second execution path: it uses `InvocationCoordinator.stream_response`,
just like `/responses`, and translates the common response events into
AG-UI wire shapes. The encoder owns no execution or continuation state.

The bridge supports client-selected threads, authored state, approval interrupts,
and results for declared client tools. Continuations use the same live stores and
execution gate as the native transports; no client-supplied identity, tool schema,
or transcript grants execution authority.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable, Mapping
from dataclasses import replace

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from .runtime_auth import principal_for
from .runtime_invocation import InvocationCoordinator
from .runtime_agui_input import AGUIInput, resolve_agui_session
from .runtime_agui_resume import pending_actions, prepare_resume
from .client_tool import PendingClientTool
from .runtime_contract import InvocationRequest


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
        envelope = AGUIInput.parse(payload)
        user_id = principal_for(request).user_id
        continuation = prepare_resume(coordinator, envelope, user_id=user_id)
        if continuation is not None:
            run_request, resume = continuation
            initializing = False
            # prepare_resume already authorizes the live principal/session pair.
            # Reading the session here would wait on the lease held by that same
            # suspended invocation, deadlocking before its result is delivered.
        else:
            run_request, initializing = await _new_request(coordinator, envelope, user_id)
            resume = None
        encoder = _AGUIEncoder(
            run_id=envelope.run_id, messages=envelope.messages, state=envelope.state,
            register_tool=lambda identity, tool_id: _register_tool(
                coordinator, user_id, run_request.session_id, identity, tool_id
            ),
            pending=lambda: pending_actions(
                coordinator, user_id=user_id, session_id=run_request.session_id
            ),
        )
        if initializing:
            # Copilot clients may initialize an empty thread before the first
            # user message. This handshake must not invoke the model.
            stream = _initial_events(encoder, run_request.session_id)
        else:
            stream = coordinator.stream_response(
                run_request, encoder=encoder.encode, continuation=resume
            )
        return StreamingResponse(stream, media_type="text/event-stream")


async def _new_request(
    coordinator: InvocationCoordinator, envelope: AGUIInput, user_id: str
) -> tuple[InvocationRequest, bool]:
    """Validate a new turn before creating its client-selected session."""

    text = envelope.text()
    if text is not None:
        coordinator.validate_input(text, require_non_empty_text=True)
    session = await resolve_agui_session(
        coordinator, user_id=user_id, thread_id=envelope.thread_id
    )
    if text is None:
        # An initialization handshake has no application input to validate.
        request = coordinator.create_request(
            "", user_id=user_id, session_id=session.id,
            metadata=envelope.metadata, transport="agui",
        )
        return request, True
    request = await coordinator.prepare_request(
        text, user_id=user_id, session_id=session.id,
        metadata=envelope.metadata, transport="agui",
    )
    return replace(request, state_delta=envelope.state), False


def _register_tool(
    coordinator: InvocationCoordinator, user_id: str, session_id: str,
    identity: str, tool_id: str,
) -> None:
    """Attach wire correlation to the actual scoped pending client call."""

    for pending in pending_actions(coordinator, user_id=user_id, session_id=session_id):
        if isinstance(pending, PendingClientTool) and pending.id == identity:
            pending.agui_tool_call_id = tool_id
            return
    raise RuntimeError("Client tool interaction is no longer available")


async def _initial_events(encoder: _AGUIEncoder, session_id: str) -> Any:
    """Initialize the protocol lifecycle without creating an empty invocation."""

    yield encoder.encode("response.created", {"sessionId": session_id})
    yield encoder.encode("response.completed", {"sessionId": session_id, "status": "completed"})


class _AGUIEncoder:
    """Track open text messages and tool calls to emit well-formed AG-UI frames."""

    def __init__(
        self, *, run_id: str, messages: list[dict[str, Any]] | None = None,
        state: Mapping[str, Any] | None = None,
        register_tool: Callable[[str, str], None] | None = None,
        pending: Callable[[], list[Any]] | None = None,
    ) -> None:
        """Keep only AG-UI framing state; the coordinator owns the run."""

        self._run_id = run_id
        self._open_message: str | None = None
        self._message_agent: str | None = None
        self._messages = [dict(message) for message in messages or []]
        self._state = dict(state or {})
        self._register_tool = register_tool
        self._pending = pending
        self._tool_calls: dict[str, Mapping[str, Any]] = {}
        self._client_calls: dict[str, str] = {}

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
        elif name == "client_tool.requested":
            events = self.close() + self._client_tool(payload["clientTool"])
        elif name == "error":
            events = self.close() + [{"type": "RUN_ERROR", "message": payload["error"]}]
        elif name in {"response.completed", "response.in_progress"}:
            events = self._terminal_events(payload)
        elif name.startswith("response."):
            events = self.translate(_neutral_event(name, payload))
        else:
            # Approval details are carried by the terminal interrupt envelope.
            events = []
        return "".join(_agui_sse(event) for event in events)

    def _terminal_events(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Map shared completion and suspension receipts to AG-UI boundaries."""

        events = self.close()
        finished = {
            "type": "RUN_FINISHED", "threadId": payload["sessionId"],
            "runId": self._run_id,
        }
        if payload.get("status") == "requires_action":
            actions = self._interrupt_actions(payload["requiredAction"])
            for action in actions:
                if action["type"] == "client_tool" and action["id"] not in self._client_calls:
                    events.extend(self._client_tool(action))
            events.extend([
                {"type": "STATE_SNAPSHOT", "snapshot": self._state},
                {"type": "MESSAGES_SNAPSHOT", "messages": self._messages},
            ])
            self._finish_actions(actions, events, finished)
        elif payload.get("status") == "in_progress":
            # External completions are application-owned. Do not offer a user
            # resume that could forge a provider result or cancel durable work.
            events.append({"type": "CUSTOM", "name": "harnest.external_wait", "value": {
                "responseId": payload["responseId"],
                "sessionId": payload["sessionId"],
                "pendingAction": payload["pendingAction"],
            }})
            finished["result"] = {"status": "in_progress", "responseId": payload["responseId"]}
        elif "result" in payload:
            finished["result"] = payload["result"]
        return events + [finished]

    def _finish_actions(self, actions: list[Mapping[str, Any]], events: list[dict], finished: dict) -> None:
        """Keep automatic frontend tools separate from human decision interrupts."""

        if any(action["type"] == "human_approval" for action in actions):
            finished["outcome"] = {
                "type": "interrupt", "interrupts": [self._interrupt(action) for action in actions],
            }
            return
        # HttpAgent blocks another run while it has unresolved interrupts.
        # A normal tool handoff lets its registered frontend handlers append
        # tool results and continue through the standard AG-UI tool loop.
        for action in actions:
            events.append({"type": "CUSTOM", "name": "harnest.client_tool", "value": {
                **dict(action), "toolCallId": self._client_tool_id(action),
            }})

    def _interrupt_actions(self, first: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        """Expose every currently open wait belonging to this exact invocation."""

        if self._pending is None:
            return [first]
        return [
            {"type": "client_tool" if isinstance(item, PendingClientTool) else "human_approval", **item.public()}
            for item in self._pending() if item.call_id == first["callId"]
        ] or [first]

    def _interrupt(self, action: Mapping[str, Any]) -> dict[str, Any]:
        """Describe only capabilities supported by Harnest's pending action."""

        interrupt = {
            "id": action["id"], "expiresAt": action["expiresAt"],
            "metadata": {"harnest": dict(action)},
        }
        if action["type"] == "human_approval":
            interrupt.update(reason="confirmation", message=action["message"], responseSchema={
                "type": "object", "properties": {"approved": {"type": "boolean"}},
                "required": ["approved"], "additionalProperties": False,
            })
        else:
            interrupt.update(reason="tool_call", toolCallId=self._client_tool_id(action))
        return interrupt

    def _client_tool_id(self, action: Mapping[str, Any]) -> str:
        """Reuse the native call identifier when the model already emitted it."""

        if action["id"] in self._client_calls:
            return self._client_calls[action["id"]]
        for identity, call in reversed(self._tool_calls.items()):
            if (identity not in self._client_calls.values()
                    and call.get("name") == action["name"] and call.get("arguments") == action["arguments"]):
                return identity
        return action["id"]

    def _client_tool(self, action: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Expose client execution without duplicating an existing tool call."""

        identity = self._client_tool_id(action)
        events = []
        if identity not in self._tool_calls:
            events = self._tool_call({"id": identity, "name": action["name"], "arguments": action["arguments"]})
        self._client_calls[action["id"]] = identity
        if self._register_tool is not None:
            self._register_tool(action["id"], identity)
        return events

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
            # A runId is caller-controlled and may be reused on retries. A fresh
            # message identity avoids overwriting an earlier turn in any client.
            self._open_message = f"msg-{uuid.uuid4().hex}"
            self._messages.append({"id": self._open_message, "role": "assistant", "content": ""})
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
            self._messages[-1]["content"] += delta
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
        self._tool_calls[call_id] = event
        self._messages.append({
            "id": uuid.uuid4().hex, "role": "assistant", "toolCalls": [{
                "id": call_id, "type": "function", "function": {
                    "name": event.get("name"), "arguments": json.dumps(event.get("arguments") or {}),
                },
            }],
        })
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
        message_id = uuid.uuid4().hex
        self._messages.append({"id": message_id, "role": "tool", "toolCallId": call_id,
                               "content": content if isinstance(content, str) else json.dumps(content)})
        self._tool_calls.pop(call_id, None)
        return [
            {
                "type": "TOOL_CALL_RESULT",
                "messageId": message_id,
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
        self._state.update(delta)
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
