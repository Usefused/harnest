"""Framework-neutral human approval declarations and execution state."""

from __future__ import annotations

import contextvars
import asyncio
import functools
import hashlib
import inspect
import json
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Iterator, Literal, TypeVar

from .logging import get_logger

F = TypeVar("F", bound=Callable[..., Any])
Decision = Literal["approve", "deny"]

_POLICY_ATTRIBUTE = "__harnest_approval__"
_WRAPPED_ATTRIBUTE = "__harnest_approval_wrapped__"
_CURRENT_EXECUTION: contextvars.ContextVar["ApprovalExecution | None"] = (
    contextvars.ContextVar("harnest_approval_execution", default=None)
)
_AUDIT = get_logger("approval.audit")


class ApprovalError(RuntimeError):
    """Base error for approval policy and state transitions."""


class ApprovalEnforcementError(ApprovalError):
    """Raised when protected work lacks a managed approval boundary."""


class ApprovalDenied(ApprovalEnforcementError):
    """Raised inside a suspended execution when its principal denies the call."""


class ApprovalExpired(ApprovalEnforcementError):
    """Raised inside a suspended execution when its decision deadline passes."""


@dataclass(frozen=True, slots=True)
class ApprovalPolicy:
    """Immutable policy attached to a local tool or MCP client factory."""

    message: str
    timeout_seconds: int = 900
    on_timeout: Literal["deny"] = "deny"
    tools: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("approval message must be a non-empty string")
        if not isinstance(self.timeout_seconds, int) or self.timeout_seconds < 1:
            raise ValueError("approval timeout_seconds must be a positive integer")
        if self.on_timeout != "deny":
            raise ValueError("approval on_timeout currently supports only 'deny'")
        if self.tools is not None:
            _validate_tool_names(self.tools)

    def applies_to(self, name: str) -> bool:
        return self.tools is None or name in self.tools


@dataclass(frozen=True, slots=True)
class ApprovalChallenge:
    """Protected action details carried out of the framework execution."""

    action: str
    argument_hash: str
    message: str
    policy: ApprovalPolicy


class ApprovalRequired(ApprovalError):
    """Stop managed execution before a protected action is called."""

    def __init__(self, challenge: ApprovalChallenge) -> None:
        super().__init__(f"human approval required for {challenge.action}")
        self.challenge = challenge


@dataclass(slots=True)
class PendingApproval:
    """One principal-scoped invocation awaiting a one-time decision."""

    id: str
    user_id: str
    session_id: str
    call_id: str
    action: str
    argument_hash: str
    message: str
    expires_at: float
    status: str = "pending"
    consumed: bool = False
    finished_at: float | None = field(default=None, repr=False)
    _store: "InMemoryApprovalStore | None" = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "callId": self.call_id,
            "action": self.action,
            "message": self.message,
            "expiresAt": datetime.fromtimestamp(
                self.expires_at, tz=timezone.utc
            ).isoformat(),
        }


@dataclass(slots=True)
class ApprovalExecution:
    """Invocation identity and optional grant visible to protected callables."""

    user_id: str
    session_id: str
    call_id: str
    grant: PendingApproval | None = None
    store: "InMemoryApprovalStore | None" = None
    run: "ApprovalRun | None" = None

    async def authorize(self, challenge: ApprovalChallenge) -> PendingApproval | None:
        if self.store is not None and self.run is not None:
            return await self.store.suspend(self.run, challenge)
        grant = self.grant
        if grant is None:
            raise ApprovalRequired(challenge)
        clock = grant._store._clock if grant._store is not None else time.time
        _consume_grant(grant, challenge, self, clock=clock)
        return grant


@dataclass(slots=True)
class ApprovalRun:
    """One live invocation whose Python task may suspend at protected calls."""

    id: str
    user_id: str
    session_id: str
    call_id: str
    notifications: asyncio.Queue[tuple[str, Any]] = field(default_factory=asyncio.Queue)
    activation: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    task: asyncio.Task[Any] | None = field(default=None, repr=False)


@dataclass(slots=True)
class _ApprovalWaiter:
    pending: PendingApproval
    run: ApprovalRun
    future: asyncio.Future[Decision]
    timer: asyncio.TimerHandle


class InMemoryApprovalStore:
    """Development approval store with atomic one-time transitions."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        max_pending: int = 1024,
        tombstone_seconds: float = 60,
    ) -> None:
        if max_pending < 1:
            raise ValueError("approval max_pending must be at least one")
        if tombstone_seconds < 0:
            raise ValueError("approval tombstone_seconds cannot be negative")
        self._clock = clock
        self._max_pending = max_pending
        self._tombstone_seconds = tombstone_seconds
        self._items: dict[str, PendingApproval] = {}
        self._waiters: dict[str, _ApprovalWaiter] = {}
        self._lock = Lock()

    def create_run(self, *, user_id: str, session_id: str, call_id: str) -> ApprovalRun:
        return ApprovalRun(
            id=f"approval_run_{uuid.uuid4().hex}",
            user_id=user_id,
            session_id=session_id,
            call_id=call_id,
        )

    def request(
        self,
        challenge: ApprovalChallenge,
        *,
        user_id: str,
        session_id: str,
        call_id: str,
        invocation: Any = None,
    ) -> PendingApproval:
        # ``invocation`` remains accepted for source compatibility, but retaining
        # request payloads in the approval store is intentionally forbidden.
        del invocation
        pending = PendingApproval(
            id=f"approval_{uuid.uuid4().hex}",
            user_id=user_id,
            session_id=session_id,
            call_id=call_id,
            action=challenge.action,
            argument_hash=challenge.argument_hash,
            message=challenge.message,
            expires_at=self._clock() + challenge.policy.timeout_seconds,
            _store=self,
        )
        with self._lock:
            self._prune_locked()
            if self._active_count_locked() >= self._max_pending:
                raise ApprovalEnforcementError("approval capacity is exhausted")
            self._items[pending.id] = pending
        _audit(pending, "requested", "committed")
        return pending

    async def suspend(
        self, run: ApprovalRun, challenge: ApprovalChallenge
    ) -> PendingApproval:
        execution = ApprovalExecution(run.user_id, run.session_id, run.call_id)
        pending, future = self._request_waiter(run, challenge)
        run.notifications.put_nowait(("approval", pending))
        try:
            decision = await future
            if decision != "approve":  # pragma: no cover - future rejects denial
                raise ApprovalDenied("approval was denied")
            with self._lock:
                _consume_grant(pending, challenge, execution, clock=self._clock)
            return pending
        finally:
            if pending.status in {"denied", "expired"}:
                self._release_waiter(pending.id)

    def _request_waiter(
        self, run: ApprovalRun, challenge: ApprovalChallenge
    ) -> tuple[PendingApproval, asyncio.Future[Decision]]:
        loop = asyncio.get_running_loop()
        pending = self.request(
            challenge,
            user_id=run.user_id,
            session_id=run.session_id,
            call_id=run.call_id,
        )
        future: asyncio.Future[Decision] = loop.create_future()
        delay = max(0.0, pending.expires_at - self._clock())
        timer = loop.call_later(delay, self._expire, pending.id)
        waiter = _ApprovalWaiter(pending, run, future, timer)
        with self._lock:
            self._waiters[pending.id] = waiter
        return pending, future

    def decide(
        self,
        approval_id: str,
        *,
        user_id: str,
        decision: Decision,
        deliver: bool = True,
    ) -> PendingApproval:
        if decision not in {"approve", "deny"}:
            raise ValueError("approval decision must be approve or deny")
        with self._lock:
            pending, waiter, expired = self._decide_locked(
                approval_id, user_id=user_id, decision=decision
            )
        if expired:
            self._complete_expiry(pending, waiter)
            raise ApprovalEnforcementError("approval is already expired")
        _audit(pending, pending.status, "committed")
        if waiter is not None:
            waiter.timer.cancel()
            if deliver:
                self.deliver_decision(pending, decision)
        return pending

    def _decide_locked(
        self, approval_id: str, *, user_id: str, decision: Decision
    ) -> tuple[PendingApproval, _ApprovalWaiter | None, bool]:
        pending = self._items.get(approval_id)
        if pending is None or pending.user_id != user_id:
            raise KeyError("approval not found")
        expired = self._mark_expired_locked(pending)
        waiter = self._waiters.get(pending.id)
        if expired:
            return pending, waiter, True
        if pending.status != "pending":
            raise ApprovalEnforcementError(f"approval is already {pending.status}")
        pending.status = "approved" if decision == "approve" else "denied"
        if decision == "deny":
            pending.finished_at = self._clock()
        return pending, waiter, False

    def deliver_decision(self, pending: PendingApproval, decision: Decision) -> None:
        with self._lock:
            waiter = self._waiters.get(pending.id)
        if waiter is None or waiter.future.done():
            return
        if decision == "approve":
            waiter.future.set_result("approve")
        else:
            waiter.future.set_exception(ApprovalDenied("approval was denied"))

    def run_for(self, pending: PendingApproval) -> ApprovalRun | None:
        with self._lock:
            waiter = self._waiters.get(pending.id)
            return None if waiter is None else waiter.run

    def assert_consumed(self, pending: PendingApproval) -> None:
        if not pending.consumed:
            raise ApprovalEnforcementError(
                "approved action was not reached during resumed execution"
            )

    def executed(self, pending: PendingApproval) -> None:
        with self._lock:
            pending.status = "executed"
            pending.finished_at = self._clock()
        _audit(pending, "executed", "committed")
        self._release_waiter(pending.id)

    def failed(self, pending: PendingApproval) -> None:
        with self._lock:
            pending.status = "execution_failed"
            pending.finished_at = self._clock()
        _audit(pending, "execution_failed", "failed")
        self._release_waiter(pending.id)

    def cancel_run(self, run: ApprovalRun) -> None:
        cancelled: list[_ApprovalWaiter] = []
        with self._lock:
            for approval_id, waiter in tuple(self._waiters.items()):
                if waiter.run is not run:
                    continue
                waiter.pending.status = "execution_cancelled"
                waiter.pending.finished_at = self._clock()
                cancelled.append(self._waiters.pop(approval_id))
            self._prune_locked()
        for waiter in cancelled:
            waiter.timer.cancel()
            _audit(waiter.pending, "execution_cancelled", "cancelled")
        task = run.task
        if task is not None and not task.done():
            task.cancel()

    @property
    def retained_count(self) -> int:
        with self._lock:
            self._prune_locked()
            return len(self._items)

    def _mark_expired_locked(self, pending: PendingApproval) -> bool:
        if pending.status == "pending" and self._clock() >= pending.expires_at:
            pending.status = "expired"
            pending.finished_at = self._clock()
            return True
        return False

    def _expire(self, approval_id: str) -> None:
        with self._lock:
            pending = self._items.get(approval_id)
            if pending is None or pending.status != "pending":
                return
            if not self._mark_expired_locked(pending):
                return
            waiter = self._waiters.get(approval_id)
        self._complete_expiry(pending, waiter)

    def _complete_expiry(
        self, pending: PendingApproval, waiter: _ApprovalWaiter | None
    ) -> None:
        _audit(pending, "expired", "committed")
        if waiter is not None and not waiter.future.done():
            waiter.future.set_exception(ApprovalExpired("approval expired"))
        self._release_waiter(pending.id)

    def _release_waiter(self, approval_id: str) -> None:
        with self._lock:
            waiter = self._waiters.pop(approval_id, None)
            self._prune_locked()
        if waiter is not None:
            waiter.timer.cancel()

    def _active_count_locked(self) -> int:
        return sum(item.status in {"pending", "approved"} for item in self._items.values())

    def _prune_locked(self) -> None:
        now = self._clock()
        removable = [
            approval_id
            for approval_id, item in self._items.items()
            if item.finished_at is not None
            and now - item.finished_at >= self._tombstone_seconds
            and approval_id not in self._waiters
        ]
        for approval_id in removable:
            self._items.pop(approval_id, None)
        self._cap_tombstones_locked()

    def _cap_tombstones_locked(self) -> None:
        terminal = sorted(
            (
                item
                for item in self._items.values()
                if item.finished_at is not None and item.id not in self._waiters
            ),
            key=lambda item: item.finished_at or 0,
        )
        for item in terminal[: -self._max_pending]:
            self._items.pop(item.id, None)


@contextmanager
def approval_execution(execution: ApprovalExecution) -> Iterator[None]:
    token = _CURRENT_EXECUTION.set(execution)
    try:
        yield
    finally:
        _CURRENT_EXECUTION.reset(token)


def require_human_approval(
    *,
    message: str,
    timeout_seconds: int = 900,
    on_timeout: Literal["deny"] = "deny",
    tools: Sequence[str] | None = None,
) -> Callable[[F], F]:
    """Declare that a tool or MCP ``client()`` factory requires approval."""

    policy = ApprovalPolicy(
        message=message,
        timeout_seconds=timeout_seconds,
        on_timeout=on_timeout,
        tools=tuple(tools) if tools is not None else None,
    )

    def decorate(function: F) -> F:
        if not callable(function):
            raise TypeError("@require_human_approval can only decorate callables")
        setattr(function, _POLICY_ATTRIBUTE, policy)
        if getattr(function, "__harnest_tool__", False):
            return wrap_approved_tool(function)
        return function

    return decorate


def approval_policy(value: Any) -> ApprovalPolicy | None:
    policy = getattr(value, _POLICY_ATTRIBUTE, None)
    return policy if isinstance(policy, ApprovalPolicy) else None


def wrap_approved_tool(function: F) -> F:
    """Install an execution gate after both decorator orders are known."""

    policy = approval_policy(function)
    if policy is None or getattr(function, _WRAPPED_ATTRIBUTE, False):
        return function
    @functools.wraps(function)
    async def async_call(*args: Any, **kwargs: Any) -> Any:
        arguments = bind_tool_arguments(function, args, kwargs)
        grant = await _authorize("tool", function.__name__, arguments, policy)
        try:
            if inspect.iscoroutinefunction(function):
                result = await function(*args, **kwargs)
            else:
                result = function(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        except BaseException:
            record_approved_failure(grant)
            raise
        _record_executed(grant)
        return result

    wrapped = async_call
    setattr(wrapped, _POLICY_ATTRIBUTE, policy)
    setattr(wrapped, _WRAPPED_ATTRIBUTE, True)
    setattr(wrapped, "__harnest_tool__", True)
    return wrapped  # type: ignore[return-value]


async def authorize_mcp(
    client_name: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    policy: ApprovalPolicy,
) -> PendingApproval | None:
    if not policy.applies_to(tool_name):
        return None
    return await _authorize(
        "mcp", f"{client_name}.{tool_name}", _bound_arguments(arguments), policy
    )


def record_approved_execution(grant: PendingApproval | None) -> None:
    _record_executed(grant)


def record_approved_failure(grant: PendingApproval | None) -> None:
    if grant is not None:
        if grant._store is not None:
            grant._store.failed(grant)
        else:
            _audit(grant, "execution_failed", "failed")


async def _authorize(
    kind: str,
    name: str,
    arguments: Mapping[str, Any],
    policy: ApprovalPolicy,
) -> PendingApproval | None:
    execution = _CURRENT_EXECUTION.get()
    if execution is None:
        raise ApprovalEnforcementError(
            f"{kind} {name!r} requires the managed Harnest approval runtime"
        )
    challenge = ApprovalChallenge(
        action=f"{kind}:{name}",
        argument_hash=_argument_hash(arguments),
        message=_render_message(policy.message, arguments),
        policy=policy,
    )
    return await execution.authorize(challenge)


def bind_tool_arguments(
    function: Callable[..., Any], args: tuple[Any, ...], kwargs: Mapping[str, Any]
) -> dict[str, Any]:
    try:
        values = inspect.signature(function).bind(*args, **kwargs).arguments
    except TypeError as exc:
        raise ApprovalEnforcementError("unable to bind protected tool arguments") from exc
    return _bound_arguments(values)


def _bound_arguments(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): _canonical(value)
        for key, value in values.items()
        if not _is_injected_framework_context(value)
    }


def _is_injected_framework_context(value: Any) -> bool:
    module = type(value).__module__
    if module.startswith("google.adk."):
        try:
            from google.adk.tools.tool_context import ToolContext
        except ImportError:  # pragma: no cover - optional backend
            return False
        return isinstance(value, ToolContext)
    if module.startswith("langgraph."):
        try:
            from langgraph.prebuilt.tool_node import ToolRuntime
        except ImportError:  # pragma: no cover - optional backend
            return False
        return isinstance(value, ToolRuntime)
    return False


def _canonical(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ApprovalEnforcementError("approval arguments must be finite")
        return value
    if isinstance(value, Mapping):
        return _canonical_mapping(value)
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _canonical(dump(mode="json"))
    raise ApprovalEnforcementError(
        f"approval cannot bind argument type {type(value).__name__}"
    )


def _canonical_mapping(value: Mapping[Any, Any]) -> dict[str, Any]:
    return {
        str(key): _canonical(item)
        for key, item in sorted(value.items(), key=lambda item: str(item[0]))
    }


def _argument_hash(arguments: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _render_message(template: str, arguments: Mapping[str, Any]) -> str:
    try:
        return template.format_map(_MessageArguments(arguments))
    except (ValueError, AttributeError) as exc:
        raise ApprovalEnforcementError("invalid approval message template") from exc


class _MessageArguments(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _record_executed(grant: PendingApproval | None) -> None:
    if grant is not None:
        if grant._store is not None:
            grant._store.executed(grant)
        else:
            _audit(grant, "executed", "committed")


def _consume_grant(
    grant: PendingApproval,
    challenge: ApprovalChallenge,
    execution: ApprovalExecution,
    *,
    clock: Callable[[], float],
) -> None:
    if grant.consumed:
        raise ApprovalEnforcementError("approval grant was already consumed")
    if grant.status != "approved":
        raise ApprovalEnforcementError(f"approval is {grant.status}, not approved")
    if clock() >= grant.expires_at:
        grant.status = "expired"
        grant.finished_at = clock()
        _audit(grant, "expired", "committed")
        raise ApprovalExpired("approval expired before protected execution")
    if (
        grant.user_id != execution.user_id
        or grant.session_id != execution.session_id
        or grant.call_id != execution.call_id
        or grant.action != challenge.action
        or grant.argument_hash != challenge.argument_hash
    ):
        raise ApprovalEnforcementError(
            "approved action did not match the suspended invocation"
        )
    grant.consumed = True


def _audit(pending: PendingApproval, operation: str, outcome: str) -> None:
    # Only stable identifiers cross the audit boundary; arguments and rendered
    # messages can contain customer data and are intentionally excluded.
    _AUDIT.info(
        "approval.%s" % operation,
        operation=operation,
        trigger="user" if operation in {"approved", "denied"} else "agent",
        outcome=outcome,
        action=pending.action,
    )


def _validate_tool_names(tools: Sequence[str]) -> None:
    if isinstance(tools, (str, bytes)) or not tools:
        raise TypeError("approval tools must be a non-empty sequence of names")
    if any(not isinstance(name, str) or not name.strip() for name in tools):
        raise ValueError("approval tool names must be non-empty strings")
    if len(set(tools)) != len(tools):
        raise ValueError("approval tool names must be unique")


__all__ = [
    "ApprovalEnforcementError",
    "ApprovalDenied",
    "ApprovalError",
    "ApprovalExpired",
    "ApprovalExecution",
    "ApprovalPolicy",
    "ApprovalRequired",
    "ApprovalRun",
    "InMemoryApprovalStore",
    "PendingApproval",
    "approval_execution",
    "approval_policy",
    "authorize_mcp",
    "bind_tool_arguments",
    "record_approved_execution",
    "record_approved_failure",
    "require_human_approval",
    "wrap_approved_tool",
]
