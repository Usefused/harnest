"""Tool authoring helpers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar, overload

from .client_tool import client_tool

F = TypeVar("F", bound=Callable[..., Any])


@overload
def tool(function: F) -> F: ...


@overload
def tool(*, description: str | None = None) -> Callable[[F], F]: ...


def tool(function: F | None = None, *, description: str | None = None):
    """Mark a typed Python function as a managed Harnest tool.

    Both managed backends wrap callables natively. This decorator preserves the
    function's typed signature, checks that the model receives a description,
    and installs declared execution policy before backend lowering.
    """

    def decorate(fn: F) -> F:
        if not callable(fn):
            raise TypeError("@tool can only decorate callables")
        from .context import registration_for as context_registration_for
        from .lifecycle import registration_for as lifecycle_registration_for

        if context_registration_for(fn) is not None:
            raise TypeError("context providers cannot also be tools")
        if lifecycle_registration_for(fn) is not None:
            raise TypeError("lifecycle extensions cannot also be tools")
        if description:
            fn.__doc__ = description
        if not (fn.__doc__ or "").strip():
            name = getattr(fn, "__name__", type(fn).__name__)
            raise ValueError(f"tool {name!r} needs a docstring or description")
        setattr(fn, "__harnest_tool__", True)
        # Approval may be written above or below @tool. Delaying the wrapper
        # choice until both markers are present keeps both natural orders safe.
        from .approval import wrap_approved_tool

        return wrap_approved_tool(fn)

    return decorate(function) if function is not None else decorate


__all__ = ["client_tool", "tool"]
