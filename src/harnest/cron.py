"""Declarative UTC schedules for compiler-discovered Harnest tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
import inspect
from types import MappingProxyType
from typing import Any, Mapping

from .task import (
    CompiledTask,
    TaskCallable,
    registration_for as task_registration_for,
    safe_task_arguments,
)


_CRON_LIMITS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


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
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    return value


__all__ = ["CompiledCron", "Cron"]
