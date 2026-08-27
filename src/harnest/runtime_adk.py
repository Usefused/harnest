"""Google ADK driver for the framework-neutral Harnest runtime.

This module is intentionally limited to translating between the neutral runtime
contract and ADK.  HTTP validation, deadlines, concurrency limits, and public
wire formats belong to :mod:`harnest.neutral_runtime`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import aclosing
from datetime import datetime, timezone
from typing import Any

from .application import CompiledApplication
from .neutral_runtime import (
    AgentInfo,
    InvocationRequest,
    InvocationResult,
    RuntimeDriver,
    SessionConflictError,
    SessionRecord,
)


class ADKRuntimeDriver(RuntimeDriver):
    """Run one compiled ADK application through an owned in-memory runner."""

    framework = "adk"

    def __init__(
        self,
        application: CompiledApplication,
        *,
        card: Mapping[str, Any] | None = None,
        extra_endpoints: Mapping[str, str] | None = None,
    ) -> None:
        if application.framework != "adk":
            raise ValueError("ADKRuntimeDriver requires an ADK application")
        if application.native_app is None:
            raise ValueError("compiled ADK application does not contain an App")

        try:
            from google.adk.runners import InMemoryRunner
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("Google ADK is required to run an ADK agent") from exc

        self.application = application
        card_data = dict(card or {})
        description = card_data.get("description")
        if not isinstance(description, str):
            description = getattr(application.target, "description", "") or ""
        display_name = card_data.get("name")
        if not isinstance(display_name, str) or not display_name.strip():
            display_name = application.name
        self._info = AgentInfo(
            id=application.name,
            name=display_name,
            description=description,
            card=card_data,
            framework=application.framework,
            mode=application.mode,
            extra_endpoints=dict(extra_endpoints or {}),
        )
        self._runner = InMemoryRunner(
            app=application.native_app,
            app_name=application.native_app.name,
        )
        self._closed = False
        self._close_lock = asyncio.Lock()

    @property
    def app_name(self) -> str:
        return self.application.native_app.name

    @property
    def info(self) -> AgentInfo:
        return self._info

    async def create_session(
        self,
        *,
        user_id: str,
        session_id: str,
        state: Mapping[str, Any],
    ) -> SessionRecord:
        self._ensure_open()
        existing = await self._runner.session_service.get_session(
            app_name=self.app_name,
            user_id=user_id,
            session_id=session_id,
        )
        if existing is not None:
            raise SessionConflictError(f"session already exists: {session_id}")
        try:
            session = await self._runner.session_service.create_session(
                app_name=self.app_name,
                user_id=user_id,
                session_id=session_id,
                state=dict(state),
            )
        except ValueError as exc:
            # Preserve the portable conflict contract if another coroutine won
            # the race between the existence check and creation.
            raise SessionConflictError(
                f"session already exists: {session_id}"
            ) from exc
        return _session_record(session)

    async def get_session(
        self, *, user_id: str, session_id: str
    ) -> SessionRecord | None:
        self._ensure_open()
        session = await self._runner.session_service.get_session(
            app_name=self.app_name,
            user_id=user_id,
            session_id=session_id,
        )
        return None if session is None else _session_record(session)

    async def list_sessions(self, *, user_id: str) -> list[SessionRecord]:
        self._ensure_open()
        response = await self._runner.session_service.list_sessions(
            app_name=self.app_name,
            user_id=user_id,
        )
        return [_session_record(session) for session in response.sessions]

    async def update_session(
        self,
        *,
        session_id: str,
        user_id: str,
        state_delta: Mapping[str, Any],
    ) -> SessionRecord | None:
        self._ensure_open()
        session = await self._runner.session_service.get_session(
            app_name=self.app_name,
            user_id=user_id,
            session_id=session_id,
        )
        if session is None:
            return None
        try:
            from google.adk.events import Event, EventActions
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("Google ADK is required to update a session") from exc
        event = Event(
            invocation_id=f"session-{uuid.uuid4().hex}",
            author="user",
            actions=EventActions(state_delta=dict(state_delta)),
        )
        await self._runner.session_service.append_event(session=session, event=event)
        updated = await self._runner.session_service.get_session(
            app_name=self.app_name,
            user_id=user_id,
            session_id=session_id,
        )
        return None if updated is None else _session_record(updated)

    async def delete_session(self, *, user_id: str, session_id: str) -> bool:
        self._ensure_open()
        existing = await self._runner.session_service.get_session(
            app_name=self.app_name,
            user_id=user_id,
            session_id=session_id,
        )
        if existing is None:
            return False
        await self._runner.session_service.delete_session(
            app_name=self.app_name,
            user_id=user_id,
            session_id=session_id,
        )
        return True

    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        """Collect one native ADK event stream into a neutral result."""

        items = [item async for item in self.stream(request)]
        text = "".join(
            item["text"]
            for item in items
            if item.get("type") == "message"
            and isinstance(item.get("text"), str)
        )
        result = next(
            (
                item.get("output")
                for item in reversed(items)
                if item.get("type") == "graph_output"
            ),
            None,
        )
        return InvocationResult(
            text=text,
            events=tuple(items),
            result=result,
            session_id=request.session_id,
            metadata=dict(request.metadata),
        )

    async def stream(
        self, request: InvocationRequest
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield privacy-safe neutral deltas from ADK's native event stream."""

        self._ensure_open()
        try:
            from google.adk.agents.run_config import RunConfig
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("Google ADK is required to run an ADK agent") from exc

        message = types.Content(
            role="user", parts=[types.Part(text=request.input)]
        )
        metadata = dict(request.metadata)
        run_config = RunConfig(custom_metadata=metadata) if metadata else None
        native_events = self._runner.run_async(
            user_id=request.user_id,
            session_id=request.session_id,
            invocation_id=request.invocation_id,
            new_message=message,
            state_delta=dict(request.state_delta),
            run_config=run_config,
        )
        normalizer = _ADKEventNormalizer()
        async with aclosing(native_events):
            async for event in native_events:
                for item in normalizer.feed(event):
                    yield item

    async def close(self) -> None:
        """Close ADK resources exactly once."""

        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            await self._runner.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("ADK runtime driver is closed")


class _ADKEventNormalizer:
    """Turn ADK cumulative/partial events into small neutral event deltas."""

    def __init__(self) -> None:
        self._text = ""

    def feed(self, event: Any) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in _event_items(event):
            if item["type"] != "message":
                normalized.append(item)
                continue
            text = item["text"]
            partial = bool(getattr(event, "partial", False))
            if self._text and text.startswith(self._text):
                delta = text[len(self._text) :]
                self._text = text
            elif not partial and self._text == text:
                delta = ""
            else:
                delta = text
                self._text = self._text + text if partial else text
            if delta:
                normalized.append(
                    {"type": "message", "role": "assistant", "text": delta}
                )
            if not partial:
                self._text = ""
        return normalized


def _event_items(event: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) if content is not None else None
    text = "".join(
        part.text
        for part in parts or ()
        if isinstance(getattr(part, "text", None), str)
        and not getattr(part, "thought", False)
    )
    if text:
        items.append({"type": "message", "role": "assistant", "text": text})

    output = getattr(event, "output", None)
    if output is not None and _is_terminal_output(event):
        if isinstance(output, str):
            if not text:
                items.append(
                    {"type": "message", "role": "assistant", "text": output}
                )
        normalized_output = _json_value(output)
        items.append(
            {
                "type": "graph_output",
                "output": normalized_output,
                "result": normalized_output,
            }
        )

    get_calls = getattr(event, "get_function_calls", None)
    for call in get_calls() if callable(get_calls) else ():
        items.append(
            {
                "type": "tool_call",
                "id": getattr(call, "id", None),
                "name": getattr(call, "name", None),
                "arguments": _json_value(getattr(call, "args", None)),
            }
        )
    get_responses = getattr(event, "get_function_responses", None)
    for response in get_responses() if callable(get_responses) else ():
        items.append(
            {
                "type": "tool_result",
                "id": getattr(response, "id", None),
                "name": getattr(response, "name", None),
                "result": _json_value(getattr(response, "response", None)),
            }
        )
    return items


def _is_terminal_output(event: Any) -> bool:
    node_info = getattr(event, "node_info", None)
    output_for = getattr(node_info, "output_for", None)
    if not output_for:
        return True
    return any(isinstance(path, str) and "/" not in path for path in output_for)


def _session_record(session: Any) -> SessionRecord:
    updated_at = _timestamp(getattr(session, "last_update_time", 0.0))
    return SessionRecord(
        id=session.id,
        user_id=session.user_id,
        state=_json_value(session.state),
        updated_at=updated_at,
    )


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _json_value(model_dump(mode="json", by_alias=True))
    return str(value)


__all__ = ["ADKRuntimeDriver"]
