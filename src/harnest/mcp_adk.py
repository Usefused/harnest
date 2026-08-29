"""Bind discovered ADK MCP tools to the managed invocation facade."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin

from .lifecycle import LifecycleListener
from .mcp import _adk_mcp_toolset_metadata
from .mcp_context import (
    _GovernedMCPTool,
    _activate_mcp_context,
    _managed_mcp_tool,
    _mark_governed_mcp_operation,
)


_ACTIVE_NATIVE_TOOL_CONTEXT: ContextVar[Any | None] = ContextVar(
    "harnest_adk_mcp_tool_context", default=None
)
_ORIGINAL_OPERATION = "__harnest_adk_mcp_original_operation__"


@dataclass(slots=True)
class _InvocationBinding:
    """Retain one revocable MCP activation until ADK completes its run."""

    activation: AbstractContextManager[Any] = field(repr=False)


class _ADKMCPContextCoordinator:
    """Discover, activate, and release per-invocation governed MCP tools."""

    def __init__(self, target: Any, listeners: Sequence[LifecycleListener]) -> None:
        self._target = target
        self._listeners = tuple(listeners)
        self._bindings: dict[str, _InvocationBinding] = {}

    async def enter(self, invocation_context: Any) -> None:
        """Discover native tools once and bind their exact governed markers."""

        invocation_id = _invocation_id(invocation_context)
        if invocation_id in self._bindings:
            raise RuntimeError("ADK MCP context is already active for this invocation")
        clients = await _discover_adk_mcp_clients(
            self._target, invocation_context
        )
        activation = _activate_mcp_context(clients, self._listeners)
        activation.__enter__()
        self._bindings[invocation_id] = _InvocationBinding(activation)

    def exit(self, invocation_context: Any) -> None:
        """Revoke native and copied facade authority after one ADK run."""

        binding = self._bindings.pop(_invocation_id(invocation_context), None)
        if binding is not None:
            binding.activation.__exit__(None, None, None)


class _ADKMCPContextEnterPlugin(BasePlugin):
    """Activate MCP capabilities before authored run callbacks execute."""

    def __init__(self, coordinator: _ADKMCPContextCoordinator) -> None:
        super().__init__(name="_harnest_mcp_context_enter")
        self._coordinator = coordinator

    async def before_run_callback(self, *, invocation_context: Any) -> None:
        """Install the invocation's discovered MCP capability registry."""

        await self._coordinator.enter(invocation_context)


class _ADKMCPContextExitPlugin(BasePlugin):
    """Keep MCP capabilities live through authored completion callbacks."""

    def __init__(self, coordinator: _ADKMCPContextCoordinator) -> None:
        super().__init__(name="_harnest_mcp_context_exit")
        self._coordinator = coordinator

    async def after_run_callback(self, *, invocation_context: Any) -> None:
        """Revoke the successful run's MCP capability registry."""

        self._coordinator.exit(invocation_context)

    async def on_run_error_callback(
        self, *, invocation_context: Any, error: Exception
    ) -> None:
        """Revoke after failure without inspecting provider error content."""

        del error
        self._coordinator.exit(invocation_context)


def adk_mcp_context_plugins(
    target: Any, listeners: Sequence[LifecycleListener]
) -> tuple[BasePlugin, BasePlugin]:
    """Create paired plugins so authored callbacks remain inside the binding."""

    coordinator = _ADKMCPContextCoordinator(target, listeners)
    return _ADKMCPContextEnterPlugin(coordinator), _ADKMCPContextExitPlugin(
        coordinator
    )


async def _discover_adk_mcp_clients(
    target: Any, invocation_context: Any
) -> dict[str, dict[str, _GovernedMCPTool]]:
    """Index toolsets in one set-based traversal and govern their cached tools."""

    from google.adk.agents.readonly_context import ReadonlyContext

    readonly = ReadonlyContext(invocation_context)
    clients: dict[str, dict[str, _GovernedMCPTool]] = {}
    for toolset in _managed_toolsets(target):
        public_name, capability_id, _approval_wrapped = (
            _adk_mcp_toolset_metadata(toolset)
        )
        if public_name in clients:
            raise ValueError("ADK MCP toolsets use a duplicate public identity")
        discovered = await toolset.get_tools_with_prefix(readonly)
        client_tools: dict[str, _GovernedMCPTool] = {}
        clients[public_name] = client_tools
        for remote_tool in discovered:
            name = _remote_tool_name(toolset, remote_tool)
            if name in client_tools:
                raise ValueError("ADK MCP client exposes a duplicate tool name")
            marker = _adk_mcp_marker(
                capability_id, name, remote_tool, invocation_context
            )
            client_tools[name] = marker
            remote_tool.run_async = _native_marker_operation(
                marker, _original_operation(remote_tool.run_async)
            )
    return clients


def _adk_mcp_marker(
    client_name: str,
    name: str,
    remote_tool: Any,
    invocation_context: Any,
) -> _GovernedMCPTool:
    """Create the shared marker around ADK's already approval-wrapped call."""

    operation = _original_operation(remote_tool.run_async)

    async def invoke(arguments: Mapping[str, Any]) -> Any:
        native_context = _ACTIVE_NATIVE_TOOL_CONTEXT.get()
        if native_context is None:
            from google.adk.tools.tool_context import ToolContext

            # Context-originated dispatch still receives an invocation-private
            # framework context without exposing it through the public facade.
            native_context = ToolContext(invocation_context)
        return await operation(
            args=dict(arguments), tool_context=native_context
        )

    # Approval, when configured, already wraps remote_tool.run_async during
    # toolset discovery. Passing no policy avoids a second authorization gate.
    return _managed_mcp_tool(client_name, name, invoke, approval=None)


def _native_marker_operation(
    marker: _GovernedMCPTool, original_operation: Any
) -> Any:
    """Adapt ADK's native signature to the exact marker used by context.mcp."""

    async def invoke(*, args: dict[str, Any], tool_context: Any) -> Any:
        with _native_tool_context(tool_context):
            return await marker.invoke(args)

    # ADK may return the same cached tool object on the next invocation. Keeping
    # its approval-wrapped original avoids nesting a fresh marker around a
    # revoked invocation marker.
    setattr(invoke, _ORIGINAL_OPERATION, original_operation)
    return _mark_governed_mcp_operation(invoke)


def _original_operation(operation: Any) -> Any:
    """Unwrap only the private routing layer installed by this adapter."""

    return getattr(operation, _ORIGINAL_OPERATION, operation)


@contextmanager
def _native_tool_context(value: Any) -> Iterator[None]:
    """Keep ADK's mutable ToolContext private to one framework-originated call."""

    token = _ACTIVE_NATIVE_TOOL_CONTEXT.set(value)
    try:
        yield
    finally:
        _ACTIVE_NATIVE_TOOL_CONTEXT.reset(token)


def _managed_toolsets(target: Any) -> tuple[Any, ...]:
    """Traverse managed agents and workflow nodes without following parents."""

    found: list[Any] = []
    pending = [target]
    seen: set[int] = set()
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        for tool in getattr(value, "tools", ()) or ():
            try:
                _adk_mcp_toolset_metadata(tool)
            except TypeError:
                continue
            found.append(tool)
        pending.extend(getattr(value, "sub_agents", ()) or ())
        graph = getattr(value, "graph", None)
        pending.extend(getattr(graph, "nodes", ()) or ())
    return tuple(found)


def _remote_tool_name(toolset: Any, remote_tool: Any) -> str:
    """Remove only the prefix added by this managed ADK toolset."""

    exposed = getattr(remote_tool, "name", None)
    if not isinstance(exposed, str) or not exposed:
        raise ValueError("ADK MCP tool is missing a stable name")
    prefix = getattr(toolset, "tool_name_prefix", None)
    marker = f"{prefix}_" if isinstance(prefix, str) and prefix else ""
    return exposed.removeprefix(marker)


def _invocation_id(invocation_context: Any) -> str:
    """Read the native correlation key without coercing framework values."""

    value = getattr(invocation_context, "invocation_id", None)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("ADK invocation id must be a non-empty string")
    return value


__all__ = ["adk_mcp_context_plugins"]
