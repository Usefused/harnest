"""Typed tools whose implementation runs in the connected client."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Iterator, TypeVar, overload

from .approval import ApprovalRun, bind_tool_arguments
from .logging import get_logger

F = TypeVar("F", bound=Callable[..., Any])
_CURRENT: contextvars.ContextVar["ClientToolExecution | None"] = contextvars.ContextVar(
    "harnest_client_tool_execution", default=None
)
_AUDIT = get_logger("client_tool.audit")


class ClientToolError(RuntimeError):
    """A client tool request could not be completed safely."""


@dataclass(slots=True)
class PendingClientTool:
    """One principal-scoped client tool call awaiting its result."""

    id: str
    user_id: str
    session_id: str
    call_id: str
    name: str
    arguments: dict[str, Any]
    expires_at: float
    run: ApprovalRun = field(repr=False)
    future: asyncio.Future[Any] = field(repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "callId": self.call_id,
            "name": self.name,
            "arguments": dict(self.arguments),
            "expiresAt": datetime.fromtimestamp(
                self.expires_at, tz=timezone.utc
            ).isoformat(),
        }


class InMemoryClientToolStore:
    """Development store for one-time client tool result delivery."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._items: dict[str, PendingClientTool] = {}
        self._lock = Lock()

    async def suspend(
        self,
        run: ApprovalRun,
        *,
        name: str,
        arguments: dict[str, Any],
        timeout_seconds: int,
    ) -> Any:
        loop = asyncio.get_running_loop()
        pending = PendingClientTool(
            id=f"client_tool_{uuid.uuid4().hex}",
            user_id=run.user_id,
            session_id=run.session_id,
            call_id=run.call_id,
            name=name,
            arguments=arguments,
            expires_at=self._clock() + timeout_seconds,
            run=run,
            future=loop.create_future(),
        )
        with self._lock:
            self._items[pending.id] = pending
        _audit(name, "requested", trigger="agent", outcome="suspended")
        run.notifications.put_nowait(("client_tool", pending))
        try:
            return await asyncio.wait_for(pending.future, timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            _audit(name, "expired", trigger="agent", outcome="failed")
            raise ClientToolError(f"client tool {name!r} timed out") from exc
        finally:
            with self._lock:
                self._items.pop(pending.id, None)

    def submit(self, request_id: str, *, user_id: str, output: Any) -> PendingClientTool:
        with self._lock:
            pending = self._items.get(request_id)
        if pending is None or pending.user_id != user_id:
            raise KeyError("client tool request not found")
        if self._clock() >= pending.expires_at:
            raise ClientToolError("client tool request is expired")
        if pending.future.done():
            raise ClientToolError("client tool result was already submitted")
        pending.future.set_result(output)
        _audit(
            pending.name,
            "result_submitted",
            trigger="user",
            outcome="committed",
        )
        return pending

    def run_for(self, pending: PendingClientTool) -> ApprovalRun:
        return pending.run


@dataclass(frozen=True, slots=True)
class ClientToolExecution:
    """Runtime capability visible only while Harnest owns an invocation."""

    store: InMemoryClientToolStore
    run: ApprovalRun

    async def execute(
        self, *, name: str, arguments: dict[str, Any], timeout_seconds: int
    ) -> Any:
        return await self.store.suspend(
            self.run,
            name=name,
            arguments=arguments,
            timeout_seconds=timeout_seconds,
        )


@contextmanager
def client_tool_execution(execution: ClientToolExecution) -> Iterator[None]:
    token = _CURRENT.set(execution)
    try:
        yield
    finally:
        _CURRENT.reset(token)


@overload
def client_tool(function: F) -> F: ...


@overload
def client_tool(
    *, description: str | None = None, timeout_seconds: int = 300
) -> Callable[[F], F]: ...


def client_tool(
    function: F | None = None,
    *,
    description: str | None = None,
    timeout_seconds: int = 300,
):
    """Declare a typed tool implemented by the HTTP or WebSocket client."""

    if not isinstance(timeout_seconds, int) or timeout_seconds < 1:
        raise ValueError("client tool timeout_seconds must be a positive integer")

    def decorate(fn: F) -> F:
        from .context import registration_for as context_registration_for
        from .lifecycle import registration_for as lifecycle_registration_for

        if context_registration_for(fn) is not None:
            raise TypeError("context providers cannot also be client tools")
        if lifecycle_registration_for(fn) is not None:
            raise TypeError("lifecycle extensions cannot also be client tools")
        if description:
            fn.__doc__ = description
        if not (inspect.getdoc(fn) or "").strip():
            raise ValueError("client tool needs a docstring or description")

        @functools.wraps(fn)
        async def invoke(*args: Any, **kwargs: Any) -> Any:
            execution = _CURRENT.get()
            if execution is None:
                raise ClientToolError(
                    f"client tool {fn.__name__!r} requires the managed Harnest runtime"
                )
            arguments = bind_tool_arguments(fn, args, kwargs)
            return await execution.execute(
                name=fn.__name__,
                arguments=arguments,
                timeout_seconds=timeout_seconds,
            )

        setattr(invoke, "__harnest_tool__", True)
        setattr(invoke, "__harnest_client_tool__", True)
        return invoke  # type: ignore[return-value]

    return decorate(function) if function is not None else decorate


def _audit(name: str, operation: str, *, trigger: str, outcome: str) -> None:
    # Arguments and client results can contain customer data, so the audit
    # boundary records only the stable declared capability name.
    _AUDIT.info(
        f"client_tool.{operation}",
        operation=operation,
        trigger=trigger,
        outcome=outcome,
        action=f"client_tool:{name}",
    )


__all__ = [
    "ClientToolError",
    "ClientToolExecution",
    "InMemoryClientToolStore",
    "PendingClientTool",
    "client_tool",
    "client_tool_execution",
]
