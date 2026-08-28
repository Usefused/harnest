"""Tool authoring helpers."""

from __future__ import annotations

from collections.abc import Callable
import functools
import inspect
from typing import Any, TypeVar, overload

from .client_tool import client_tool
from .structured import (
    PydanticModel,
    callable_output_schema,
    validate_output_schema,
    validate_output_value,
)

F = TypeVar("F", bound=Callable[..., Any])


@overload
def tool(function: F) -> F: ...


@overload
def tool(
    *, description: str | None = None, output_schema: PydanticModel | None = None
) -> Callable[[F], F]: ...


def tool(
    function: F | None = None,
    *,
    description: str | None = None,
    output_schema: PydanticModel | None = None,
):
    """Mark a typed Python function as a managed Harnest tool.

    Both managed backends wrap callables natively. This decorator preserves the
    function's typed signature, checks that the model receives a description,
    validates an optional Pydantic result, and installs declared execution
    policy before backend lowering.
    """

    configured_schema = validate_output_schema(
        output_schema, field_name="tool output_schema"
    )

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
        schema = configured_schema or callable_output_schema(fn)
        wrapped = _validated_tool(fn, schema) if schema is not None else fn
        setattr(wrapped, "__harnest_tool__", True)
        if schema is not None:
            setattr(wrapped, "__harnest_output_schema__", schema)
        # Approval may be written above or below @tool. Delaying the wrapper
        # choice until both markers are present keeps both natural orders safe.
        from .approval import wrap_approved_tool

        return wrap_approved_tool(wrapped)

    return decorate(function) if function is not None else decorate


def _validated_tool(function: F, schema: PydanticModel) -> F:
    """Wrap a tool without changing whether callers await its implementation."""

    if inspect.iscoroutinefunction(function):

        @functools.wraps(function)
        async def async_call(*args: Any, **kwargs: Any) -> Any:
            value = await function(*args, **kwargs)
            return validate_output_value(
                schema, value, boundary=f"tool {function.__name__!r} output"
            )

        wrapped = async_call
    else:

        @functools.wraps(function)
        def sync_call(*args: Any, **kwargs: Any) -> Any:
            value = function(*args, **kwargs)
            return validate_output_value(
                schema, value, boundary=f"tool {function.__name__!r} output"
            )

        wrapped = sync_call
    # Framework tool builders inspect the return annotation to advertise the
    # same schema that Harnest enforces after execution.
    wrapped.__annotations__ = {
        **getattr(function, "__annotations__", {}),
        "return": schema,
    }
    wrapped.__signature__ = inspect.signature(function).replace(  # type: ignore[attr-defined]
        return_annotation=schema
    )
    return wrapped  # type: ignore[return-value]


__all__ = ["client_tool", "tool"]
