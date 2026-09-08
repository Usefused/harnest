"""Declarative UTC schedules for compiler-discovered Harnest tasks."""

from __future__ import annotations

import builtins
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import inspect
import re
from types import MappingProxyType
from typing import Any, Iterator, Mapping

from .cron_storage import CronRecord, CronStore, CronStoreConflictError
from .task import (
    CompiledTask,
    TaskCallable,
    registration_for as task_registration_for,
    safe_task_arguments,
)


_CRON_LIMITS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_SCHEDULE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:~-]{0,127}$")
_SCHEDULE_ID = re.compile(r"^cron_[a-f0-9]{32}$")
_ACTIVE_RUNTIME: ContextVar[Any | None] = ContextVar(
    "harnest_cron_runtime", default=None
)
_UNSET = object()


class CronUnavailableError(RuntimeError):
    """Raised when dynamic schedules are used outside a cron-enabled runtime."""


class CronRuntimeError(RuntimeError):
    """Raised when durable cron persistence or dispatch fails safely."""


class CronConflictError(RuntimeError):
    """Raised when a schedule key already owns a different definition."""


class CronNotFoundError(LookupError):
    """Raised when a user-owned dynamic schedule no longer exists."""


@dataclass(frozen=True, slots=True)
class Cron:
    """Schedule one discovered task using a strict five-column UTC expression."""

    schedule: str
    task: TaskCallable[Any] = field(repr=False, compare=False)
    arguments: Mapping[str, Any] = field(
        default_factory=dict, repr=False, compare=False
    )
    timezone: str = "UTC"

    def __post_init__(self) -> None:
        """Reject invalid schedules and calls before compiler discovery proceeds."""

        _validate_schedule(self.schedule)
        if self.timezone != "UTC":
            raise ValueError("cron timezone must be UTC")
        if task_registration_for(self.task) is None:
            raise TypeError("cron task must be a Harnest @task callable")
        arguments = safe_task_arguments(self.arguments)
        _validate_task_call(self.task, arguments)
        # Cron declarations may live for the process lifetime after compilation;
        # freezing nested containers prevents later imports changing queued work.
        object.__setattr__(self, "arguments", _freeze_mapping(arguments))


@dataclass(frozen=True, slots=True)
class CompiledCron:
    """One compiler-resolved schedule with a stable application-owned identity."""

    name: str
    source: str
    schedule: str
    timezone: str
    task: CompiledTask = field(repr=False, compare=False)
    arguments: Mapping[str, Any] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        """Defend direct application construction at the compiler/runtime seam."""

        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("compiled cron name must be non-empty text")
        if not isinstance(self.source, str) or not self.source.startswith("cron/"):
            raise ValueError("compiled cron source must be below cron/")
        _validate_schedule(self.schedule)
        if self.timezone != "UTC":
            raise ValueError("compiled cron timezone must be UTC")
        if not isinstance(self.task, CompiledTask):
            raise TypeError("compiled cron task must be a CompiledTask")
        arguments = safe_task_arguments(self.arguments)
        _validate_task_call(self.task.authored, arguments)
        object.__setattr__(self, "arguments", _freeze_mapping(arguments))

    @property
    def task_name(self) -> str:
        """Return the compiler-owned native task name targeted by the schedule."""

        return self.task.name


@dataclass(frozen=True, slots=True)
class CronJob:
    """One user-owned recurring schedule retained by a running application."""

    id: str
    key: str
    expression: str
    task_name: str
    arguments: Mapping[str, Any] = field(repr=False, compare=False)
    status: str
    timezone: str = "UTC"
    _runtime: Any = field(repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        """Validate records crossing the private runtime boundary."""

        _validate_schedule_id(self.id)
        _validate_schedule_key(self.key)
        _validate_schedule(self.expression)
        if self.timezone != "UTC":
            raise ValueError("cron timezone must be UTC")
        if not isinstance(self.task_name, str) or not self.task_name.strip():
            raise ValueError("cron task name must be non-empty text")
        if self.status not in {"active", "paused", "cancelled"}:
            raise ValueError("cron job status must be active, paused, or cancelled")
        object.__setattr__(
            self, "arguments", _freeze_mapping(safe_task_arguments(self.arguments))
        )

    async def update(
        self,
        *,
        expression: str | None = None,
        arguments: Mapping[str, Any] | object = _UNSET,
    ) -> CronJob:
        """Replace supplied fields while preserving this schedule's identity."""

        return await _schedule_runtime(self._runtime).update_dynamic_schedule(
            self.id, expression=expression, arguments=arguments
        )

    async def pause(self) -> CronJob:
        """Stop future occurrences without deleting the schedule."""

        return await _schedule_runtime(self._runtime).set_dynamic_schedule_status(
            self.id, "paused"
        )

    async def resume(self) -> CronJob:
        """Resume future occurrences from the next matching UTC minute."""

        return await _schedule_runtime(self._runtime).set_dynamic_schedule_status(
            self.id, "active"
        )

    async def cancel(self) -> CronJob:
        """Permanently stop future occurrences while retaining this record."""

        return await _schedule_runtime(self._runtime).set_dynamic_schedule_status(
            self.id, "cancelled"
        )

    async def delete(self) -> bool:
        """Delete this schedule within the currently active owner scope."""

        return await _schedule_runtime(self._runtime).delete_dynamic_schedule(self.id)


async def create(
    *,
    key: str,
    expression: str,
    task: TaskCallable[Any],
    arguments: Mapping[str, Any] | None = None,
) -> CronJob:
    """Persist an idempotently keyed schedule for the active invocation user."""

    _validate_schedule_key(key)
    _validate_schedule(expression)
    if task_registration_for(task) is None:
        raise TypeError("dynamic cron task must be a Harnest @task callable")
    normalized = safe_task_arguments({} if arguments is None else arguments)
    _validate_task_call(task, normalized)
    return await _schedule_runtime().create_dynamic_schedule(
        key=key, expression=expression, task=task, arguments=normalized
    )


async def get(schedule_id: str) -> CronJob | None:
    """Return one schedule owned by the active invocation user."""

    _validate_schedule_id(schedule_id)
    return await _schedule_runtime().get_dynamic_schedule(schedule_id)


async def list(
    *, after: str | None = None, limit: int = 50
) -> tuple[CronJob, ...]:
    """Return a bounded stable page of schedules for the active user."""

    if after is not None:
        _validate_schedule_id(after)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("cron schedule limit must be between 1 and 100")
    return await _schedule_runtime().list_dynamic_schedules(after=after, limit=limit)


async def update(
    schedule_id: str,
    *,
    expression: str | None = None,
    arguments: Mapping[str, Any] | object = _UNSET,
) -> CronJob:
    """Update one user-owned schedule through the active runtime."""

    _validate_schedule_id(schedule_id)
    if expression is not None:
        _validate_schedule(expression)
    if arguments is not _UNSET:
        safe_task_arguments(arguments)  # type: ignore[arg-type]
    return await _schedule_runtime().update_dynamic_schedule(
        schedule_id, expression=expression, arguments=arguments
    )


async def pause(schedule_id: str) -> CronJob:
    """Pause one user-owned schedule."""

    _validate_schedule_id(schedule_id)
    return await _schedule_runtime().set_dynamic_schedule_status(
        schedule_id, "paused"
    )


async def resume(schedule_id: str) -> CronJob:
    """Resume one user-owned schedule."""

    _validate_schedule_id(schedule_id)
    return await _schedule_runtime().set_dynamic_schedule_status(
        schedule_id, "active"
    )


async def cancel(schedule_id: str) -> CronJob:
    """Permanently stop future occurrences while retaining the job record."""

    _validate_schedule_id(schedule_id)
    return await _schedule_runtime().set_dynamic_schedule_status(
        schedule_id, "cancelled"
    )


async def delete(schedule_id: str) -> bool:
    """Delete one user-owned schedule."""

    _validate_schedule_id(schedule_id)
    return await _schedule_runtime().delete_dynamic_schedule(schedule_id)


@contextmanager
def _activate_runtime(runtime: Any) -> Iterator[None]:
    """Make one task runtime available only for its managed execution scope."""

    token = _ACTIVE_RUNTIME.set(runtime)
    try:
        yield
    finally:
        _ACTIVE_RUNTIME.reset(token)


def _schedule_runtime(expected: Any | None = None) -> Any:
    """Require matching runtime and invocation scopes before durable access."""

    from . import context
    from .context import ContextUnavailableError

    try:
        context.current()
    except ContextUnavailableError:
        raise CronUnavailableError(
            "dynamic cron requires an active managed Harnest invocation"
        ) from None
    active = _ACTIVE_RUNTIME.get()
    if active is None:
        raise CronUnavailableError(
            "dynamic cron requires a cron-enabled durable task runtime"
        )
    if expected is not None and active is not expected:
        raise CronUnavailableError("cron schedule belongs to a different runtime")
    return active


def _validate_task_call(
    task_value: TaskCallable[Any], arguments: Mapping[str, Any]
) -> None:
    """Require static cron arguments to satisfy the authored task signature."""

    try:
        inspect.signature(task_value).bind(**dict(arguments))
    except TypeError as error:
        raise TypeError(
            f"cron arguments do not match task signature: {error}"
        ) from None


def _validate_schedule(value: Any) -> None:
    """Validate the numeric five-column subset Harnest guarantees for MVP."""

    if not isinstance(value, str) or value != value.strip():
        raise ValueError(
            "cron schedule must be non-empty text without outer whitespace"
        )
    fields = value.split()
    if len(fields) != len(_CRON_LIMITS):
        raise ValueError("cron schedule must contain exactly five columns")
    for field, limits in zip(fields, _CRON_LIMITS):
        _validate_field(field, limits)


def _validate_schedule_key(value: Any) -> None:
    """Keep caller idempotency keys bounded and safe for durable indexing."""

    if not isinstance(value, str) or _SCHEDULE_KEY.fullmatch(value) is None:
        raise ValueError(
            "cron schedule key must start with a letter or number and contain only "
            "letters, numbers, '.', '_', ':', '~', or '-'"
        )


def _validate_schedule_id(value: Any) -> None:
    """Reject forged identifiers before issuing an owner-scoped query."""

    if not isinstance(value, str) or _SCHEDULE_ID.fullmatch(value) is None:
        raise ValueError("cron schedule id is invalid")


def _matches_schedule(expression: str, timestamp: int) -> bool:
    """Match one UTC minute through the parser used by static task schedules."""

    _validate_schedule(expression)
    if not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp < 0:
        raise ValueError("cron timestamp must be a non-negative integer")
    try:
        from croniter import croniter
    except ImportError:
        raise CronUnavailableError(
            "dynamic cron requires the durable task runtime dependencies"
        ) from None
    previous = croniter(expression, timestamp + 1).get_prev(ret_type=float)
    return round(previous) == timestamp


def _validate_field(value: str, limits: tuple[int, int]) -> None:
    """Validate one comma-separated cron field without importing the backend."""

    parts = value.split(",")
    if not parts or any(not part for part in parts):
        raise ValueError(f"invalid cron field {value!r}")
    for part in parts:
        _validate_field_part(part, limits)


def _validate_field_part(value: str, limits: tuple[int, int]) -> None:
    """Validate a wildcard, number, or ascending range with an optional step."""

    base, separator, step = value.partition("/")
    if separator and not _valid_step(step):
        raise ValueError(f"invalid cron step {value!r}")
    if base == "*":
        return
    start, separator, end = base.partition("-")
    if not _is_ascii_decimal(start) or (separator and not _is_ascii_decimal(end)):
        raise ValueError(f"invalid cron field component {value!r}")
    lower = int(start)
    upper = int(end) if separator else lower
    if not limits[0] <= lower <= upper <= limits[1]:
        raise ValueError(f"cron field component is out of range: {value!r}")


def _valid_step(value: str) -> bool:
    """Accept one positive ASCII step without a second separator."""

    return "/" not in value and _is_ascii_decimal(value) and int(value) >= 1


def _is_ascii_decimal(value: str) -> bool:
    """Keep compiler/runtime behavior independent of Unicode digit parsing."""

    return value.isascii() and value.isdecimal()


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Deep-freeze validated JSON arguments retained by a compiled application."""

    return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})


def _freeze_value(value: Any) -> Any:
    """Freeze only containers because scalar JSON values are already immutable."""

    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, builtins.list):
        return tuple(_freeze_value(item) for item in value)
    return value


__all__ = [
    "CompiledCron",
    "Cron",
    "CronConflictError",
    "CronJob",
    "CronNotFoundError",
    "CronRuntimeError",
    "CronRecord",
    "CronStore",
    "CronStoreConflictError",
    "CronUnavailableError",
    "cancel",
    "create",
    "delete",
    "get",
    "list",
    "pause",
    "resume",
    "update",
]
