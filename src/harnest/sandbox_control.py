"""Share admission, cancellation, and deadlines across framework worker threads."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time
from typing import Any, Iterator

from .context import ContextUnavailableError, optional_active_context


class SandboxCancelledError(RuntimeError):
    """An execution lost its caller or its managed invocation authority."""

    from .sandbox_types import SandboxStatus
    status = SandboxStatus.CANCELLED


class SandboxControl:
    """Keep absolute deadlines and captured authority independent of worker context."""

    def __init__(self, timeout_seconds: int | None, *, parent: SandboxControl | None = None) -> None:
        """Share captured authority and cancellation, but own a scope-local deadline."""
        self._parent = parent
        self.cancelled = parent.cancelled if parent is not None else threading.Event()
        self._deadline: float | None = None
        self._managed_context = parent._managed_context if parent is not None else optional_active_context()
        self.constrain(timeout_seconds)

    @property
    def deadline(self) -> float | None:
        """Cap local time by live ancestor deadlines without modifying their budgets."""
        inherited = self._parent.deadline if self._parent is not None else None
        if self._deadline is None:
            return inherited
        return self._deadline if inherited is None else min(self._deadline, inherited)

    @deadline.setter
    def deadline(self, value: float | None) -> None:
        """Set only this scope's limit; ancestor limits continue to apply."""
        self._deadline = value

    def constrain(self, timeout_seconds: int | None) -> None:
        """Tighten this scope without extending an inherited absolute deadline."""
        if timeout_seconds is not None:
            candidate = time.monotonic() + timeout_seconds
            self.deadline = candidate if self.deadline is None else min(self.deadline, candidate)

    def remaining(self) -> float | None:
        """Return remaining admission and execution time without resetting the clock."""
        return None if self.deadline is None else max(0.0, self.deadline - time.monotonic())

    def check(self) -> None:
        """Fail closed even in watchdog threads without the caller's ContextVar."""
        if self.cancelled.is_set():
            raise SandboxCancelledError("sandbox execution was cancelled")
        if self._managed_context is not None:
            try:
                self._managed_context._require_active()
            except ContextUnavailableError:
                raise SandboxCancelledError("sandbox invocation is no longer active") from None
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise TimeoutError("sandbox execution deadline expired")

    @contextmanager
    def acquire(self, lock: Any) -> Iterator[None]:
        """Make queued admission cancellable and revalidate before side effects."""
        self.check()
        while not lock.acquire(timeout=self._poll_interval()):
            self.check()
        try:
            # Cancellation can race with lock release, so polling alone is not
            # sufficient to prevent stale queued work from reaching a provider.
            self.check()
            yield
        finally:
            lock.release()

    def _poll_interval(self) -> float:
        """Bound cancellation latency without polling faster than the deadline."""
        remaining = self.remaining()
        return 0.05 if remaining is None else min(0.05, remaining)


_CONTROL: ContextVar[SandboxControl | None] = ContextVar("harnest_sandbox_control", default=None)


def current_control() -> SandboxControl | None:
    """Return the execution token inherited by native framework worker threads."""
    return _CONTROL.get()


@contextmanager
def execution_control(timeout_seconds: int | None) -> Iterator[SandboxControl]:
    """Scope deadlines independently while preserving call-wide cancellation."""
    # Restoring a shared object's deadline would race with sibling workers and
    # extend the budget of workers that retained the shorter helper context.
    control = SandboxControl(timeout_seconds, parent=current_control())
    token = _CONTROL.set(control)
    try:
        control.check()
        yield control
    except BaseException:
        control.cancelled.set()
        raise
    finally:
        _CONTROL.reset(token)
