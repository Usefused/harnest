"""Ephemeral client input consumed by application code, never by a model."""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, get_type_hints

from pydantic import BaseModel

from .client_tool import ClientToolError, _CURRENT
from .structured import PydanticModel, validate_output_schema


class _PrivateDelivery:
    """Keep a future's result opaque and empty it as ownership is transferred."""

    __slots__ = ("_value",)

    def __init__(self, value: BaseModel) -> None:
        self._value: BaseModel | None = value

    def take(self) -> BaseModel:
        """Transfer the validated value once without retaining a second copy."""

        value, self._value = self._value, None
        if value is None:
            raise ClientToolError("private client input was already consumed")
        return value

    def clear(self) -> None:
        """Release an undelivered value when the owning invocation ends."""

        self._value = None

    def __reduce_ex__(self, protocol: int) -> Any:
        """Reject checkpoint serialization rather than persisting private input."""

        raise TypeError("private client input cannot be serialized")


def _prepare_private_input(schema: PydanticModel, output: Any) -> _PrivateDelivery:
    """Validate privately without preserving value-bearing exception chains."""

    try:
        return _PrivateDelivery(schema.model_validate(output))
    except Exception:
        # Validators can embed raw input in arbitrary errors. Raise outside
        # the handler so neither cause nor context reaches framework telemetry.
        output = None
    raise ClientToolError("private client input does not match its declared schema")


def client_input(
    *,
    input_schema: PydanticModel,
    response: Mapping[str, Any],
    description: str | None = None,
    timeout_seconds: int = 300,
    permission: str | None = None,
) -> Callable[[Callable[..., Awaitable[None]]], Callable[..., Awaitable[dict[str, Any]]]]:
    """Collect private input for an async handler and return only authored JSON.

    The handler's first positional parameter receives the validated private
    input and is excluded from the model tool signature. Remaining parameters
    are public request arguments. The handler must return None. ``response``
    is frozen at declaration time and is the only successful model result.
    """

    schema = validate_output_schema(input_schema, field_name="client input input_schema")
    if schema is None:
        raise TypeError("client input requires an input_schema")
    if type(timeout_seconds) is not int or timeout_seconds < 1:
        raise ValueError("client input timeout_seconds must be a positive integer")
    if not isinstance(response, Mapping):
        raise TypeError("client input response must be a JSON object")
    encoded_response = json.dumps(dict(response), allow_nan=False)
    if permission is not None:
        from .agent_principal import validate_permission

        validate_permission(permission)

    def decorate(handler: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[dict[str, Any]]]:
        """Separate the private handler signature from its public tool wrapper."""

        signature = _public_signature(handler, description)

        @functools.wraps(handler)
        async def invoke(*args: Any, **kwargs: Any) -> dict[str, Any]:
            """Keep raw delivery inside the application-only consumer boundary."""

            from .agent_principal import require_capability
            from .durable import current_native_durable_call

            require_capability(invoke, name=handler.__name__)
            if current_native_durable_call() is not None:
                raise ClientToolError("private client input cannot run inside a durable tool")
            execution = _CURRENT.get()
            if execution is None:
                raise ClientToolError("private client input requires the managed Harnest runtime")
            arguments = signature.bind(*args, **kwargs)
            arguments.apply_defaults()
            value = await execution.execute(
                name=handler.__name__, arguments=dict(arguments.arguments),
                output_schema=schema, timeout_seconds=timeout_seconds, private_input=True,
            )
            try:
                await _consume_private(handler, value, args, kwargs)
            finally:
                value = None
            return json.loads(encoded_response)

        invoke.__signature__ = signature  # type: ignore[attr-defined]
        invoke.__annotations__ = {
            key: parameter.annotation for key, parameter in signature.parameters.items()
            if parameter.annotation is not inspect.Parameter.empty
        }
        invoke.__annotations__["return"] = dict[str, Any]
        setattr(invoke, "__harnest_tool__", True)
        setattr(invoke, "__harnest_client_input__", True)
        from .agent.approval import wrap_approved_tool
        from .tool_lifecycle import wrap_lifecycle_tool

        if permission is not None:
            from .agent_principal import attach_required_permissions

            # Attach before wrapping so every approval and lifecycle layer
            # receives the permission marker, not just the outer callable.
            attach_required_permissions(invoke, (permission,))
        return wrap_lifecycle_tool(wrap_approved_tool(invoke))

    return decorate


def _public_signature(handler: Any, description: str | None) -> inspect.Signature:
    """Reject ambiguous authoring before hiding the private first parameter."""

    from ._agent_tool import _validate_tool_authoring

    _validate_tool_authoring(handler, description)
    if not inspect.iscoroutinefunction(handler):
        raise TypeError("client input handlers must be async functions")
    if getattr(handler, "__harnest_tool__", False):
        raise TypeError("client input handlers cannot also be tools")
    signature = inspect.signature(handler)
    parameters = list(signature.parameters.values())
    if not parameters or parameters[0].kind not in {
        inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD,
    }:
        raise TypeError("client input handler requires a first positional private input parameter")
    if parameters[0].default is not inspect.Parameter.empty:
        raise TypeError("private input parameter cannot have a default")
    if any(parameter.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
           for parameter in parameters):
        raise TypeError("client input handlers require explicit public parameters")
    return signature.replace(parameters=_resolved_parameters(handler, parameters[1:]), return_annotation=dict[str, Any])


def _resolved_parameters(handler: Any, parameters: list[inspect.Parameter]) -> list[inspect.Parameter]:
    """Resolve public annotations in the author's module before wrapping it."""

    try:
        hints = get_type_hints(handler)
    except (NameError, TypeError):
        hints = {}
    return [parameter.replace(annotation=hints.get(parameter.name, parameter.annotation)) for parameter in parameters]


async def _consume_private(
    handler: Callable[..., Awaitable[None]], value: BaseModel,
    args: tuple[Any, ...], kwargs: dict[str, Any],
) -> None:
    """Discard handler returns and sanitize failures before framework observers."""

    cancelled = False
    result = None
    try:
        result = await handler(value, *args, **kwargs)
        if result is None:
            return
    except asyncio.CancelledError:
        cancelled = True
    except BaseException:
        pass
    finally:
        value = result = None
    if cancelled:
        raise asyncio.CancelledError
    # No exception or returned object from trusted code is projected into a
    # tool result, lifecycle hook, or error trace by the managed framework.
    raise ClientToolError("private client input handler failed or returned a value")


__all__ = ["client_input"]
