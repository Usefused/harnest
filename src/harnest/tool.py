"""Tool authoring helpers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar, overload

F = TypeVar("F", bound=Callable[..., Any])


@overload
def tool(function: F) -> F: ...


@overload
def tool(*, description: str | None = None) -> Callable[[F], F]: ...


def tool(function: F | None = None, *, description: str | None = None):
    """Mark a typed Python function as an ADK tool.

    ADK already auto-wraps callables. This decorator keeps the function intact,
    checks that the model will receive a description, and optionally supplies a
    docstring when one cannot be written inline.
    """

    def decorate(fn: F) -> F:
        if not callable(fn):
            raise TypeError("@tool can only decorate callables")
        if description:
            fn.__doc__ = description
        if not (fn.__doc__ or "").strip():
            name = getattr(fn, "__name__", type(fn).__name__)
            raise ValueError(f"tool {name!r} needs a docstring or description")
        setattr(fn, "__harnest_tool__", True)
        return fn

    return decorate(function) if function is not None else decorate
