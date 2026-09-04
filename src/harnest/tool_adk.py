"""Apply portable tool lifecycle policy to native Google ADK tools."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin

from .context import activate_context, context, derive_agent_context
from .lifecycle import LifecycleListener
from .mcp_context import _is_governed_mcp_operation
from .tool_arguments import callable_argument_names, unknown_argument_error
from .tool_lifecycle import (
    ToolCallRequest,
    ToolLifecycleContext,
    ToolLifecyclePipeline,
)


class ADKPortableToolLifecyclePlugin(BasePlugin):
    """Intercept native tools not already wrapped by managed Harnest policy."""

    def __init__(self, listeners: Sequence[LifecycleListener]) -> None:
        super().__init__(name="_harnest_portable_tool_lifecycle")
        self._pipeline = ToolLifecyclePipeline(listeners)

    async def before_model_callback(
        self, *, callback_context: Any, llm_request: Any
    ) -> None:
        """Remove permissioned capabilities from this invocation's model view."""

        del callback_context
        _project_model_tools(llm_request)

    async def before_tool_callback(
        self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any
    ) -> Any:
        """Transform or finish one native call before transport execution."""

        from .agent_principal import require_capability

        require_capability(tool, name=_tool_name(tool))

        argument_error = unknown_argument_error(
            _tool_name(tool), tool_args, _declared_argument_names(tool)
        )
        if argument_error is not None:
            # ADK filters kwargs before invoking FunctionTool callables. Returning
            # an error here keeps an invented parameter visible to the model so it
            # can repair the call instead of mistaking an ignored filter for success.
            return {"error": argument_error}
        if _already_governed(tool):
            return None
        request = ToolCallRequest(_tool_name(tool), (), tool_args)
        with _tool_agent_scope(tool_context):
            lifecycle_context = _lifecycle_context(tool_context, request.name)
            try:
                updated, finished = await self._pipeline._before(
                    lifecycle_context, request
                )
                if finished is not None:
                    return await self._pipeline._after(
                        lifecycle_context, finished.result
                    )
            except BaseException as error:
                await self._pipeline._notify(lifecycle_context, error)
                raise
        if updated is not request:
            # ADK intentionally propagates in-place changes to later plugins and
            # the native tool; returning a mapping would short-circuit execution.
            tool_args.clear()
            tool_args.update(dict(updated.kwargs))
        return None

    async def after_tool_callback(
        self,
        *,
        tool: Any,
        tool_args: dict[str, Any],
        tool_context: Any,
        result: Any,
    ) -> Any:
        """Transform native output once and preserve later plugins if unchanged."""

        del tool_args
        if _already_governed(tool):
            return None
        with _tool_agent_scope(tool_context):
            lifecycle_context = _lifecycle_context(
                tool_context, _tool_name(tool)
            )
            try:
                updated = await self._pipeline._after(lifecycle_context, result)
            except BaseException as error:
                await self._pipeline._notify(lifecycle_context, error)
                raise
        return None if updated is result else updated

    async def on_tool_error_callback(
        self,
        *,
        tool: Any,
        tool_args: dict[str, Any],
        tool_context: Any,
        error: Exception,
    ) -> None:
        """Notify portable observers while leaving ADK's failure authoritative."""

        del tool_args
        if _already_governed(tool):
            return
        with _tool_agent_scope(tool_context):
            await self._pipeline._notify(
                _lifecycle_context(tool_context, _tool_name(tool)), error
            )


def adk_tool_lifecycle_plugin(
    listeners: Sequence[LifecycleListener],
) -> BasePlugin:
    """Create one framework-wide adapter for portable tool listeners."""

    return ADKPortableToolLifecyclePlugin(listeners)


def _already_governed(tool: Any) -> bool:
    """Avoid a second lifecycle around @tool and shared MCP markers."""

    operation = getattr(tool, "run_async", None)
    if _is_governed_mcp_operation(operation):
        return True
    function = getattr(tool, "func", None)
    return getattr(function, "__harnest_tool_lifecycle_wrapped__", False) is True


def _project_model_tools(llm_request: Any) -> None:
    """Filter ADK's invocation-local declarations without mutating the agent."""

    from .agent_principal import capability_is_available

    tools = getattr(llm_request, "tools_dict", None)
    if not isinstance(tools, dict):
        return
    unavailable = {
        name for name, tool in tools.items() if not capability_is_available(tool)
    }
    if not unavailable:
        return
    llm_request.tools_dict = {
        name: tool for name, tool in tools.items() if name not in unavailable
    }
    _project_tool_declarations(getattr(llm_request, "config", None), unavailable)


def _project_tool_declarations(config: Any, unavailable: set[str]) -> None:
    """Keep ADK's provider declarations aligned with its runtime tool index."""

    for tool_group in getattr(config, "tools", None) or ():
        declarations = getattr(tool_group, "function_declarations", None)
        if declarations is None:
            continue
        tool_group.function_declarations = [
            declaration
            for declaration in declarations
            if getattr(declaration, "name", None) not in unavailable
        ]


@contextmanager
def _tool_agent_scope(tool_context: Any) -> Iterator[None]:
    """Prefer ADK's executing agent identity without changing invocation tenancy."""

    active = context.current()
    agent_name = getattr(tool_context, "agent_name", None)
    if not isinstance(agent_name, str) or not agent_name or agent_name == active.agent_name:
        yield
        return
    with activate_context(derive_agent_context(active, agent_name=agent_name)):
        yield


def _lifecycle_context(
    tool_context: Any, tool_name: str
) -> ToolLifecycleContext:
    """Build portable identity from the active revocable invocation authority."""

    active = context.current()
    agent_name = getattr(tool_context, "agent_name", None)
    return ToolLifecycleContext(
        framework=active.framework,
        agent_name=agent_name if isinstance(agent_name, str) and agent_name else active.agent_name,
        invocation_id=active.invocation_id,
        user_id=active.user_id,
        session_id=active.session_id,
        tool_name=tool_name,
    )


def _tool_name(tool: Any) -> str:
    """Read ADK's declared name without reflecting arbitrary tool objects."""

    value = getattr(tool, "name", None)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("ADK tool name must be a non-empty string")
    return value


def _declared_argument_names(tool: Any) -> frozenset[str] | None:
    """Read callable or declaration inputs while preserving open native tools."""

    function = getattr(tool, "func", None)
    if callable(function):
        return callable_argument_names(
            function, ignored=getattr(tool, "_ignore_params", ())
        )
    declaration_factory = getattr(tool, "_get_declaration", None)
    if not callable(declaration_factory):
        return None
    declaration = declaration_factory()
    return _declaration_argument_names(declaration)


def _declaration_argument_names(declaration: Any) -> frozenset[str] | None:
    """Read declared native-tool properties while respecting an explicit open map."""

    if declaration is None:
        return None
    json_schema = getattr(declaration, "parameters_json_schema", None)
    if isinstance(json_schema, Mapping):
        additional = json_schema.get("additionalProperties")
        if additional is True or isinstance(additional, Mapping):
            return None
        properties = json_schema.get("properties", {})
        return _property_names(properties)
    properties = getattr(getattr(declaration, "parameters", None), "properties", None)
    return _property_names(properties)


def _property_names(properties: Any) -> frozenset[str] | None:
    """Normalize one ADK schema property mapping without inspecting its values."""

    if not isinstance(properties, Mapping):
        return None
    return frozenset(str(name) for name in properties)


__all__ = ["ADKPortableToolLifecyclePlugin", "adk_tool_lifecycle_plugin"]
