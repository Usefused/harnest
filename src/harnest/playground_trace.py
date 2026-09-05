"""Bounded, principal-scoped runtime traces for the development playground."""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from ._json import json_value
from .output import _agent_metadata_from_runtime_event
from .runtime_contract import (
    AgentInfo,
    InvocationRequest,
    InvocationResult,
    RuntimeDriver,
    RuntimeEvent,
    SessionMessage,
    SessionRecord,
)


_ACTIVE_TRACE: contextvars.ContextVar[tuple["PlaygroundTraceStore", str] | None] = (
    contextvars.ContextVar("harnest_playground_trace", default=None)
)
_STANDARD_LOG_FIELDS = frozenset(logging.makeLogRecord({}).__dict__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_detail(event: RuntimeEvent) -> dict[str, Any]:
    """Summarize runtime activity without retaining generated content or state."""

    event_type = event.get("type")
    if event_type == "message":
        return {
            "characters": len(str(event.get("text", ""))),
            **_trace_agent(event),
        }
    if event_type == "thinking":
        # Reasoning is public on the response stream but remains content-free in
        # the longer-lived diagnostic trace, just like answer and tool payloads.
        return {
            "characters": len(str(event.get("text", ""))),
            **_trace_agent(event),
        }
    if event_type == "agent_activity":
        return {
            "activity": event.get("activity"),
            **_trace_agent(event),
        }
    if event_type == "agent_metadata":
        return _agent_metadata_trace(event)
    if event_type == "tool_call":
        return {
            "name": event.get("name"),
            "argumentFields": _mapping_field_count(event.get("arguments")),
            **_trace_agent(event),
        }
    if event_type == "tool_result":
        return {
            "name": event.get("name"),
            "outputType": _value_shape(
                event.get("result", event.get("output"))
            ),
            **_trace_agent(event),
        }
    return {"kind": event_type}


def _agent_metadata_trace(event: RuntimeEvent) -> dict[str, Any]:
    """Retain normalized diagnostics while never copying opt-in raw metadata."""

    metadata = _agent_metadata_from_runtime_event(event).as_dict()
    usage = metadata.pop("usage", None)
    metadata.pop("raw", None)
    detail = {**metadata, **_trace_agent(event)}
    if isinstance(usage, Mapping):
        detail.update(usage)
    if "raw" in event:
        detail["rawAvailable"] = True
    return detail


def _trace_agent(event: Mapping[str, Any]) -> dict[str, str]:
    """Keep only a non-empty normalized agent label in trace details."""

    agent = event.get("agent")
    return {"agent": agent} if isinstance(agent, str) and agent else {}


def _mapping_field_count(value: Any) -> int | None:
    """Describe tool arguments without retaining their names or values."""

    return len(value) if isinstance(value, Mapping) else None


def _value_shape(value: Any) -> str:
    """Classify a tool result without retaining custom type or content data."""

    if value is None:
        return "null"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return "array"
    return "scalar"


class PlaygroundTraceStore:
    """Keep a bounded in-memory trace history for one development server."""

    def __init__(self, limit: int = 50) -> None:
        if limit < 1:
            raise ValueError("trace limit must be positive")
        self._limit = limit
        self._traces: dict[str, dict[str, Any]] = {}
        self._order: deque[str] = deque()
        self._lock = threading.Lock()

    def start(self, request: InvocationRequest, transport: str) -> None:
        trace = {
            "id": request.invocation_id,
            "sessionId": request.session_id,
            "userId": request.user_id,
            "transport": transport,
            "status": "running",
            "startedAt": _now(),
            "startedMonotonic": time.monotonic(),
            "endedAt": None,
            "durationMs": None,
            "entries": [],
        }
        with self._lock:
            self._traces[request.invocation_id] = trace
            self._order.append(request.invocation_id)
            while len(self._order) > self._limit:
                self._traces.pop(self._order.popleft(), None)
        self.add(request.invocation_id, "runtime", "Invocation entered runtime")

    def add(
        self,
        trace_id: str,
        category: str,
        message: str,
        *,
        level: str = "INFO",
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        with self._lock:
            trace = self._traces.get(trace_id)
            if trace is None:
                return
            offset = max(0.0, time.monotonic() - trace["startedMonotonic"])
            trace["entries"].append(
                {
                    "timestamp": _now(),
                    "offsetMs": round(offset * 1000, 1),
                    "category": category,
                    "level": level,
                    "message": message,
                    "detail": json_value(dict(detail or {}), unsupported="string"),
                }
            )

    def event(self, trace_id: str, event: RuntimeEvent) -> None:
        event_type = str(event.get("type", "runtime_event"))
        self.add(
            trace_id,
            "tool" if event_type.startswith("tool_") else "runtime",
            event_type.replace("_", " ").capitalize(),
            detail=_event_detail(event),
        )

    def finish(self, trace_id: str, status: str, error: BaseException | None = None) -> None:
        if error is not None:
            self.add(
                trace_id,
                "error",
                str(error),
                level="ERROR",
                detail={"type": type(error).__name__},
            )
        self.add(trace_id, "runtime", f"Invocation {status}")
        with self._lock:
            trace = self._traces.get(trace_id)
            if trace is None or trace["endedAt"] is not None:
                return
            elapsed = max(0.0, time.monotonic() - trace["startedMonotonic"])
            trace["status"] = status
            trace["endedAt"] = _now()
            trace["durationMs"] = round(elapsed * 1000, 1)

    def list(self, *, user_id: str, session_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            values = [
                self._public(self._traces[trace_id])
                for trace_id in reversed(self._order)
                if self._traces[trace_id]["userId"] == user_id
                and (session_id is None or self._traces[trace_id]["sessionId"] == session_id)
            ]
        return values

    def get(self, trace_id: str, *, user_id: str) -> dict[str, Any] | None:
        with self._lock:
            trace = self._traces.get(trace_id)
            if trace is None or trace["userId"] != user_id:
                return None
            return self._public(trace)

    @staticmethod
    def _public(trace: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: json_value(value, unsupported="string")
            for key, value in trace.items()
            if key not in {"userId", "startedMonotonic"}
        }


class _TraceLogHandler(logging.Handler):
    def __init__(self, store: PlaygroundTraceStore) -> None:
        super().__init__(logging.NOTSET)
        self.store = store

    def emit(self, record: logging.LogRecord) -> None:
        active = _ACTIVE_TRACE.get()
        if active is None or active[0] is not self.store:
            return
        attributes = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_LOG_FIELDS and not key.startswith("_")
        }
        self.store.add(
            active[1],
            "log",
            record.getMessage(),
            level=record.levelname,
            detail={"logger": record.name, **attributes},
        )


class PlaygroundTraceRuntimeDriver(RuntimeDriver):
    """Observe one runtime driver without changing its public results."""

    def __init__(self, driver: RuntimeDriver, store: PlaygroundTraceStore) -> None:
        self._driver = driver
        self.store = store
        self._handler = _TraceLogHandler(store)
        logging.getLogger("harnest.agent").addHandler(self._handler)

    @property
    def info(self) -> AgentInfo:
        return self._driver.info

    async def create_session(
        self, *, session_id: str, user_id: str, state: Mapping[str, Any]
    ) -> SessionRecord:
        return await self._driver.create_session(
            session_id=session_id, user_id=user_id, state=state
        )

    async def get_session(
        self, *, session_id: str, user_id: str
    ) -> SessionRecord | None:
        return await self._driver.get_session(session_id=session_id, user_id=user_id)

    async def list_sessions(
        self,
        *,
        user_id: str,
        after: str | None = None,
        limit: int | None = None,
    ) -> Sequence[SessionRecord]:
        """Delegate session keyset bounds without adding trace observations."""

        if after is None and limit is None:
            return await self._driver.list_sessions(user_id=user_id)
        return await self._driver.list_sessions(
            user_id=user_id, after=after, limit=limit
        )

    async def get_session_messages(
        self, *, session_id: str, user_id: str
    ) -> Sequence[SessionMessage] | None:
        """Delegate transcript reads without adding trace observations."""

        return await self._driver.get_session_messages(
            session_id=session_id, user_id=user_id
        )

    async def update_session(
        self,
        *,
        session_id: str,
        user_id: str,
        state_delta: Mapping[str, Any],
    ) -> SessionRecord | None:
        return await self._driver.update_session(
            session_id=session_id, user_id=user_id, state_delta=state_delta
        )

    async def delete_session(self, *, session_id: str, user_id: str) -> bool:
        return await self._driver.delete_session(session_id=session_id, user_id=user_id)

    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        self.store.start(request, request.transport or "response")
        token = _ACTIVE_TRACE.set((self.store, request.invocation_id))
        try:
            result = await self._driver.invoke(request)
            for event in result.events:
                self.store.event(request.invocation_id, event)
            self.store.finish(request.invocation_id, "completed")
            return result
        except Exception as error:
            self.store.finish(request.invocation_id, "failed", error)
            raise
        finally:
            _ACTIVE_TRACE.reset(token)

    async def stream(self, request: InvocationRequest) -> AsyncIterator[RuntimeEvent]:
        self.store.start(request, request.transport or "stream")
        token = _ACTIVE_TRACE.set((self.store, request.invocation_id))
        status = "cancelled"
        error: BaseException | None = None
        try:
            async for event in self._driver.stream(request):
                self.store.event(request.invocation_id, event)
                yield event
            status = "completed"
        except Exception as caught:
            status = "failed"
            error = caught
            raise
        finally:
            self.store.finish(request.invocation_id, status, error)
            _ACTIVE_TRACE.reset(token)

    async def close(self) -> None:
        logging.getLogger("harnest.agent").removeHandler(self._handler)
        await self._driver.close()


__all__ = [
    "PlaygroundTraceRuntimeDriver",
    "PlaygroundTraceStore",
]
