"""Framework-neutral queued task authoring without eager queue imports."""

from __future__ import annotations

from dataclasses import dataclass, field
import inspect
import math
import re
from threading import RLock
from typing import Any, Callable, Generic, Mapping, TypeVar, overload


F = TypeVar("F", bound=Callable[..., Any])
_TASK_ATTRIBUTE = "__harnest_task_definition__"
_QUEUE = re.compile(r"^[A-Za-z][A-Za-z0-9._~-]{0,63}$")
_RUNTIME_LOCK = RLock()
_RUNTIMES: dict[int, Any] = {}


class TaskUnavailableError(RuntimeError):
    """A task was deferred without its compiled runtime being active."""


@dataclass(frozen=True, slots=True)
class TaskDefinition:
    """Authoring metadata retained on a decorated function."""

    function: Callable[..., Any] = field(repr=False, compare=False)
    queue: str
    max_retries: int


@dataclass(frozen=True, slots=True)
class CompiledTask:
    """One compiler-named task executable in the compiled application runtime."""

    name: str
    source: str
    definition: TaskDefinition = field(repr=False, compare=False)
    authored: TaskCallable[Any] = field(repr=False, compare=False)

    @property
    def function(self) -> Callable[..., Any]:
        """Return the original callable only at the trusted worker boundary."""

        return self.definition.function

    @property
    def queue(self) -> str:
        """Return the authored queue without exposing the definition object."""

        return self.definition.queue

    @property
    def max_retries(self) -> int:
        """Return the bounded retry count used during native registration."""

        return self.definition.max_retries


@dataclass(frozen=True, slots=True)
class TaskHandle:
    """Opaque queue handle returned after a job is committed."""

    id: str
    task_name: str
    _runtime: Any = field(repr=False, compare=False)
    _payload_id: str = field(repr=False, compare=False)
    _trigger: str = field(repr=False, compare=False)

    async def status(self) -> str:
        """Read the native job state through the owning runtime."""

        return await self._runtime.status(self)

    async def cancel(self) -> bool:
        """Cancel queued work without exposing the backend application."""

        return await self._runtime.cancel(self)

    async def result(self) -> Any:
        """Return completed work or suspend through a durable managed tool."""

        return await self._runtime.result(self)


class TaskCallable(Generic[F]):
    """Preserve direct invocation while adding an asynchronous defer boundary."""

    def __init__(self, definition: TaskDefinition) -> None:
        """Copy introspection metadata without wrapping task execution."""

        self.__harnest_task_definition__ = definition
        self.__wrapped__ = definition.function
        self.__name__ = definition.function.__name__
        self.__qualname__ = definition.function.__qualname__
        self.__module__ = definition.function.__module__
        self.__doc__ = definition.function.__doc__
        self.__annotations__ = dict(
            getattr(definition.function, "__annotations__", {})
        )
        self.__signature__ = inspect.signature(definition.function)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Execute inline when the author deliberately calls the task itself."""

        return self.__wrapped__(*args, **kwargs)

    async def defer(
        self,
        *args: Any,
        idempotency_key: str | None = None,
        schedule_in: float | None = None,
        **kwargs: Any,
    ) -> TaskHandle:
        """Commit safe arguments to the active compiled task runtime."""

        _validate_defer_options(idempotency_key, schedule_in)
        runtime = _runtime_for(self)
        arguments = _bound_arguments(self.__signature__, args, kwargs)
        return await runtime.defer(
            self,
            arguments,
            idempotency_key=idempotency_key,
            schedule_in=schedule_in,
        )


@overload
def task(function: F) -> TaskCallable[F]: ...


@overload
def task(
    *, queue: str = "default", max_retries: int = 3
) -> Callable[[F], TaskCallable[F]]: ...


def task(
    function: F | None = None,
    *,
    queue: str = "default",
    max_retries: int = 3,
) -> Any:
    """Mark a callable for queue, scheduling, and retry execution.

    Procrastinate is intentionally absent from this module. The compiler adds
    and the runtime imports that backend only when a ``tasks/`` export exists.
    Direct calls remain ordinary Python calls; ``defer`` is queue execution and
    never promises restoration of a suspended Python frame.
    """

    _validate_options(queue, max_retries)

    def decorate(value: F) -> TaskCallable[F]:
        _validate_function(value)
        return TaskCallable(TaskDefinition(value, queue, max_retries))

    return decorate(function) if function is not None else decorate


def registration_for(value: Any) -> TaskDefinition | None:
    """Return task metadata without accepting imported aliases accidentally."""

    definition = getattr(value, _TASK_ATTRIBUTE, None)
    return definition if isinstance(definition, TaskDefinition) else None


def bind_task_runtime(task_value: TaskCallable[Any], runtime: Any) -> None:
    """Bind one compiled callable only for its application runtime lifetime."""

    with _RUNTIME_LOCK:
        identity = id(task_value)
        if identity in _RUNTIMES and _RUNTIMES[identity] is not runtime:
            raise RuntimeError("compiled task is already active in another runtime")
        _RUNTIMES[identity] = runtime


def release_task_runtime(task_value: TaskCallable[Any], runtime: Any) -> None:
    """Release only the runtime that owns this compiled task binding."""

    with _RUNTIME_LOCK:
        if _RUNTIMES.get(id(task_value)) is runtime:
            _RUNTIMES.pop(id(task_value), None)


def safe_task_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize strict JSON containers without dataclass or string fallbacks."""

    if not isinstance(arguments, Mapping):
        raise TypeError("task arguments must be a mapping")
    return _safe_mapping(arguments, active=set())


def safe_task_result(value: Any) -> Any:
    """Normalize a task result to the strict JSON values safe for persistence."""

    return _safe_value(value, active=set())


def _runtime_for(task_value: TaskCallable[Any]) -> Any:
    """Resolve an application binding without retaining it on authored code."""

    with _RUNTIME_LOCK:
        runtime = _RUNTIMES.get(id(task_value))
    if runtime is None:
        raise TaskUnavailableError(
            "task defer requires an active compiled Harnest runtime"
        )
    return runtime


def _bound_arguments(
    signature: inspect.Signature, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    """Bind calls once so workers receive keyword-only JSON arguments."""

    bound = signature.bind(*args, **kwargs)
    values = dict(bound.arguments)
    variadic = next(
        (
            name
            for name, parameter in signature.parameters.items()
            if parameter.kind is inspect.Parameter.VAR_KEYWORD
        ),
        None,
    )
    if variadic is not None:
        values.update(values.pop(variadic, {}))
    return safe_task_arguments(values)


def _safe_mapping(value: Mapping[str, Any], *, active: set[int]) -> dict[str, Any]:
    """Reject key coercion and recursive customer payloads."""

    if any(not isinstance(key, str) for key in value):
        raise TypeError("task argument object keys must be strings")
    return _safe_container(
        value,
        lambda: {key: _safe_value(item, active=active) for key, item in value.items()},
        active=active,
    )


def _safe_value(value: Any, *, active: set[int]) -> Any:
    """Accept only values whose JSON representation cannot reveal hidden fields."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("task argument numbers must be finite")
        return value
    if isinstance(value, Mapping):
        return _safe_mapping(value, active=active)
    if isinstance(value, (list, tuple)):
        return _safe_container(
            value,
            lambda: [_safe_value(item, active=active) for item in value],
            active=active,
        )
    raise TypeError(
        f"task arguments must contain only JSON values; got {type(value).__name__}"
    )


def _safe_container(value: Any, factory: Callable[[], Any], *, active: set[int]) -> Any:
    """Evaluate a container once while rejecting cycles."""

    identity = id(value)
    if identity in active:
        raise TypeError("recursive task arguments are not supported")
    active.add(identity)
    try:
        return factory()
    finally:
        active.remove(identity)


def _validate_function(function: Any) -> None:
    """Require a documented callable with a queue-safe deferred signature."""

    if not callable(function):
        raise TypeError("@task can only decorate callables")
    if not (getattr(function, "__doc__", None) or "").strip():
        raise ValueError("tasks need a docstring")
    unsupported = {
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.VAR_POSITIONAL,
    }
    if any(
        parameter.kind in unsupported
        for parameter in inspect.signature(function).parameters.values()
    ):
        raise TypeError(
            "deferred tasks cannot declare positional-only or *args parameters"
        )


def _validate_options(queue: Any, max_retries: Any) -> None:
    """Keep native queue and retry metadata bounded and deterministic."""

    if not isinstance(queue, str) or _QUEUE.fullmatch(queue) is None:
        raise ValueError("task queue must be a valid storage-style identifier")
    if (
        not isinstance(max_retries, int)
        or isinstance(max_retries, bool)
        or not 0 <= max_retries <= 100
    ):
        raise ValueError("task max_retries must be an integer from 0 to 100")


def _validate_defer_options(
    idempotency_key: Any, schedule_in: Any
) -> None:
    """Bound scheduling controls before they reach the durable queue."""

    if idempotency_key is not None and (
        not isinstance(idempotency_key, str)
        or not idempotency_key.strip()
        or len(idempotency_key) > 512
    ):
        raise ValueError(
            "task idempotency_key must be non-empty text of at most 512 characters"
        )
    if schedule_in is not None and (
        isinstance(schedule_in, bool)
        or not isinstance(schedule_in, (int, float))
        or not math.isfinite(schedule_in)
        or schedule_in < 0
    ):
        raise ValueError("task schedule_in must be a finite non-negative number")


__all__ = [
    "CompiledTask",
    "TaskCallable",
    "TaskDefinition",
    "TaskHandle",
    "TaskUnavailableError",
    "safe_task_result",
    "task",
]
