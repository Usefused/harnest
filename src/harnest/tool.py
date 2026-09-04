"""Tool authoring helpers."""

from __future__ import annotations

from collections.abc import Callable
import functools
import inspect
from typing import Any, TypeVar, overload

from .client_tool import (
    ClientToolError, ClientToolExecution, InMemoryClientToolStore, PendingClientTool,
    client_tool, client_tool_execution, current_transient_media,
)
from .tool_lifecycle import ToolCallRequest, ToolLifecycleContext, ToolLifecycleError
from .structured import (
    PydanticModel,
    callable_output_schema,
    validate_output_schema,
    validate_output_value,
)
from .assets import AssetScope
from .stored_media import model_uses_stored, stage_stored_media

F = TypeVar("F", bound=Callable[..., Any])


@overload
def tool(function: F) -> F: ...


@overload
def tool(
    *,
    description: str | None = None,
    output_schema: PydanticModel | None = None,
    durable: bool = False,
) -> Callable[[F], F]: ...


def tool(
    function: F | None = None,
    *,
    description: str | None = None,
    output_schema: PydanticModel | None = None,
    durable: bool = False,
):
    """Mark a typed Python function as a managed Harnest tool.

    Both managed backends wrap callables natively. This decorator preserves the
    function's typed signature, checks that the model receives a description,
    validates an optional Pydantic result, and installs declared execution
    policy before backend lowering. Durable tools cross a framework-native
    suspension boundary and therefore must be asynchronous.
    """

    if type(durable) is not bool:
        raise TypeError("tool durable must be a boolean")

    configured_schema = validate_output_schema(
        output_schema, field_name="tool output_schema"
    )

    def decorate(fn: F) -> F:
        _validate_tool_authoring(fn, description)
        if durable and not inspect.iscoroutinefunction(fn):
            raise TypeError("durable tools must be async functions")
        schema = configured_schema or callable_output_schema(fn)
        _validate_stored_tool(fn, schema)
        wrapped = _validated_tool(fn, schema) if schema is not None else fn
        setattr(wrapped, "__harnest_tool__", True)
        setattr(wrapped, "__harnest_durable_tool__", durable)
        if schema is not None:
            setattr(wrapped, "__harnest_output_schema__", schema)
        # Approval may be written above or below @tool. Delaying the wrapper
        # choice until both markers are present keeps both natural orders safe.
        from .approval import wrap_approved_tool
        from .tool_lifecycle import wrap_lifecycle_tool

        # The lifecycle wrapper stays outside approval and validation so policy
        # can short-circuit harmlessly, while every executed side effect still
        # crosses the existing approval and schema boundaries.
        return wrap_lifecycle_tool(wrap_approved_tool(wrapped))

    return decorate(function) if function is not None else decorate


def _validate_tool_authoring(function: Any, description: str | None) -> None:
    """Apply callable, decorator-exclusivity, and documentation checks."""

    if not callable(function):
        raise TypeError("@tool can only decorate callables")
    from .context import registration_for as context_registration_for
    from .lifecycle import registration_for as lifecycle_registration_for

    if context_registration_for(function) is not None:
        raise TypeError("context providers cannot also be tools")
    if lifecycle_registration_for(function) is not None:
        raise TypeError("lifecycle extensions cannot also be tools")
    if description:
        function.__doc__ = description
    if not (function.__doc__ or "").strip():
        name = getattr(function, "__name__", type(function).__name__)
        raise ValueError(f"tool {name!r} needs a docstring or description")


def _validate_stored_tool(function: Any, schema: PydanticModel | None) -> None:
    """Keep synchronous tool call semantics stable for durable media authors."""

    if schema is None or not model_uses_stored(schema):
        return
    if inspect.iscoroutinefunction(function):
        return
    raise TypeError(
        "Stored media requires an async @tool function so asset storage can be awaited"
    )


def _validated_tool(function: F, schema: PydanticModel) -> F:
    """Wrap a tool without changing whether callers await its implementation."""

    if inspect.iscoroutinefunction(function):

        @functools.wraps(function)
        async def async_call(*args: Any, **kwargs: Any) -> Any:
            value = await function(*args, **kwargs)
            validated = validate_output_value(
                schema, value, boundary=f"tool {function.__name__!r} output"
            )
            access = current_transient_media()
            if access is None:
                return validated
            from .context import context

            active = context.current()
            validated = await stage_stored_media(
                validated,
                stores=active._asset_stores,
                scope=AssetScope(active.user_id, active.session_id),
            )
            return access.stage(validated)

        wrapped = async_call
    else:

        @functools.wraps(function)
        def sync_call(*args: Any, **kwargs: Any) -> Any:
            value = function(*args, **kwargs)
            validated = validate_output_value(
                schema, value, boundary=f"tool {function.__name__!r} output"
            )
            access = current_transient_media()
            return validated if access is None else access.stage(validated)

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


__all__ = [
    "client_tool", "tool", "ClientToolError", "ClientToolExecution",
    "InMemoryClientToolStore", "PendingClientTool", "client_tool_execution",
    "current_transient_media", "ToolCallRequest", "ToolLifecycleContext", "ToolLifecycleError",
]
