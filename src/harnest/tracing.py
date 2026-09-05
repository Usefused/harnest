"""Framework-neutral manual tracing helpers for authored agent code."""

from __future__ import annotations

import functools
import inspect
import json
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping, TypeVar

from opentelemetry import trace


_T = TypeVar("_T")


def _attributes(values: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not key.strip():
            raise TypeError("span attribute names must be non-empty strings")
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            result[key.strip()] = value
        elif isinstance(value, (list, tuple)) and all(
            isinstance(item, (str, int, float, bool)) for item in value
        ):
            result[key.strip()] = tuple(value)
        elif isinstance(value, Mapping):
            result[key.strip()] = json.dumps(value, sort_keys=True, default=str)
        else:
            result[key.strip()] = str(value)
    return result


def _tracer(name: str, version: str | None = None) -> Any:
    from .telemetry import get_tracer

    return get_tracer(name, version=version)


class Tracer:
    """Dynamic tracer that remains valid before runtime configuration."""

    def __init__(self, name: str, version: str | None = None) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("tracer name must be a non-empty string")
        self.name = name.strip()
        self.version = version

    def start_as_current_span(self, name: str, **kwargs: Any) -> Any:
        """Start and activate a span using the current telemetry provider."""

        return _tracer(self.name, self.version).start_as_current_span(name, **kwargs)

    def start_span(self, name: str, **kwargs: Any) -> Any:
        """Start a span without making it current in this execution context."""

        return _tracer(self.name, self.version).start_span(name, **kwargs)


def get_tracer(name: str = "agent", version: str | None = None) -> Tracer:
    """Return a dynamic tracer safe to create at module import time."""

    return Tracer(name, version)


@contextmanager
def span(
    name: str,
    *,
    attributes: Mapping[str, Any] | None = None,
    kind: Any | None = None,
    **attribute_values: Any,
) -> Iterator[Any]:
    """Start a current agent span carrying structured attributes."""

    if not isinstance(name, str) or not name.strip():
        raise ValueError("span name must be a non-empty string")
    values = dict(attributes or {})
    values.update(attribute_values)
    options: dict[str, Any] = {"attributes": _attributes(values)}
    if kind is not None:
        options["kind"] = kind
    with _tracer("harnest.agent").start_as_current_span(
        name.strip(), **options
    ) as current:
        yield current


def traced(
    name: str | None = None,
    *,
    attributes: Mapping[str, Any] | None = None,
    kind: Any | None = None,
    **attribute_values: Any,
) -> Callable[[_T], _T]:
    """Trace a synchronous or asynchronous authored function."""

    def decorate(function: _T) -> _T:
        if not callable(function):
            raise TypeError("traced can decorate only callables")
        span_name = name or getattr(function, "__qualname__", "agent.function")
        span_options = {
            "attributes": {**dict(attributes or {}), **attribute_values},
            "kind": kind,
        }
        if inspect.isasyncgenfunction(function):

            @functools.wraps(function)
            async def async_generator_wrapper(*args: Any, **kwargs: Any):
                with span(span_name, **span_options):
                    async for item in function(*args, **kwargs):
                        yield item

            return async_generator_wrapper  # type: ignore[return-value]
        if inspect.iscoroutinefunction(function):

            @functools.wraps(function)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with span(span_name, **span_options):
                    return await function(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        if inspect.isgeneratorfunction(function):

            @functools.wraps(function)
            def generator_wrapper(*args: Any, **kwargs: Any):
                with span(span_name, **span_options):
                    yield from function(*args, **kwargs)

            return generator_wrapper  # type: ignore[return-value]

        @functools.wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with span(span_name, **span_options):
                return function(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorate


def current_trace_ids() -> tuple[str | None, str | None]:
    """Return zero-padded W3C trace and span identifiers when recording."""

    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None, None
    return f"{context.trace_id:032x}", f"{context.span_id:016x}"


__all__ = ["Tracer", "current_trace_ids", "get_tracer", "span", "traced"]
