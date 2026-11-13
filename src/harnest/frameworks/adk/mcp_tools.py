"""Bind discovered ADK MCP tools to the managed invocation facade."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...mcp import MCPClient

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset

from ...agent.approval import ApprovalPolicy
from ...lifecycle import LifecycleListener
from ...mcp import (
    _MCPClientLifecycleBinding,
    _invoke_governed_mcp_call,
    _validate_approval_tools,
    _validate_mcp_permission_tools,
)
from ...mcp_capability_tools import _check_names, capability_functions
from ...mcp_lifecycle import attach_mcp_lifecycle
from ...mcp_context import (
    _GovernedMCPTool,
    _activate_mcp_context,
    _managed_mcp_tool,
    _mark_governed_mcp_operation,
)
from ...agent_principal import required_permissions


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
            toolset_metadata(toolset)
        )
        if public_name in clients:
            raise ValueError("ADK MCP toolsets use a duplicate public identity")
        discovered = await _context_tools(toolset, readonly)
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


async def _context_tools(toolset: Any, readonly: Any) -> list[Any]:
    """Keep native markers shared while adding developer-only governed operations."""

    discovered = await toolset.get_tools_with_prefix(readonly)
    context_tools = getattr(toolset, "get_context_tools", None)
    if context_tools is None:
        return discovered
    exposed = {_remote_tool_name(toolset, tool) for tool in discovered}
    return [*discovered, *(tool for tool in context_tools(discovered)
                          if _remote_tool_name(toolset, tool) not in exposed)]


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
    return _managed_mcp_tool(
        client_name,
        name,
        invoke,
        approval=None,
        required_permissions=tuple(required_permissions(remote_tool)),
    )


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
                toolset_metadata(tool)
            except TypeError:
                continue
            found.append(tool)
        pending.extend(getattr(value, "sub_agents", ()) or ())
        graph = getattr(value, "graph", None)
        pending.extend(getattr(graph, "nodes", ()) or ())
    return tuple(found)


def _remote_tool_name(toolset: Any, remote_tool: Any) -> str:
    """Keep local capability aliases stable while removing remote presentation prefixes."""

    capability = getattr(remote_tool, "__harnest_mcp_capability__", None)
    if capability is not None:
        return capability
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


def http_mcp_tool(schema: Any, calls: Any) -> Any:
    """Reuse ADK schema conversion while replacing only its initialized transport."""

    from google.adk.tools.mcp_tool.mcp_tool import McpTool

    class HTTPTool(McpTool):
        """Governance remains in the enclosing toolset and invocation facade."""

        async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:
            """Return MCP content without opening a 2025 session."""

            result = await calls.call_tool(schema.name, args)
            return result.model_dump(mode="json", by_alias=True, exclude_none=True)

    return HTTPTool(mcp_tool=schema, mcp_session_manager=None)


_ADK_PUBLIC_NAME = "__harnest_mcp_public_name__"
_ADK_CLIENT_NAME = "__harnest_mcp_client_name__"
_ADK_APPROVAL_WRAPPED = "__harnest_mcp_approval_wrapped__"


def build_toolset(client: Any) -> Any:
    """Build ADK's MCP toolset for one MCPClient with portable expansion and failure rules."""

    classes = _mcp_classes()
    binding = client._lifecycle_binding("adk")
    toolset = _construct_toolset(client, classes, binding)
    _attach_toolset_metadata(
        toolset,
        public_name=client.identity or client.tool_name_prefix or client._client_name(),
        client_name=client._client_name(),
        approval_wrapped=client.approval is not None,
    )
    for runtime_binding in client._runtime_bindings("adk"):
        attach_mcp_lifecycle(toolset, runtime_binding)
    return toolset


def _construct_toolset(client: Any, classes: tuple[type[Any], ...], binding: _MCPClientLifecycleBinding | None) -> BaseToolset:
    """Keep portable parameter and constructor failures inside their component."""
    try:
        connection = _connection(client, classes, binding=binding)
        selected = toolset_type(client, classes[0])
        if client.portable is not None:
            selected = portable_adk_toolset(selected, client)
        return selected(
            connection_params=connection,
            tool_filter=list(client.tool_filter) if client.tool_filter is not None else None,
            tool_name_prefix=client.tool_name_prefix,
        )
    except Exception as error:
        if client.portable is None:
            raise
        client.portable.failed(error)
        return disabled_adk_toolset()

def toolset_type(client: Any, base: Any) -> Any:
    """Project remote tools and local context operations through one client policy."""

    configured = client
    policy = client.approval
    required_permission = client.permission
    tool_permissions = dict(client.tool_permissions)
    client_name = client._client_name()
    public_name = client.identity or client.tool_name_prefix or client_name

    class GovernedMcpToolset(base):
        """Apply approval and principal projection after ADK discovery."""

        # Invocation adapters need the canonical capability key without
        # retaining the MCPClient, whose fields include raw connection data.
        __harnest_mcp_public_name__ = public_name
        __harnest_mcp_client_name__ = client_name
        __harnest_mcp_approval_wrapped__ = policy is not None

        async def get_tools(self, readonly_context: Any = None) -> list[Any]:
            """Apply client authorization before discovery, then project each operation."""

            from ...agent_principal import (
                attach_required_permissions,
                capability_is_available,
                permissions_are_available,
            )

            client_requirements = (
                () if required_permission is None else (required_permission,)
            )
            # Even an otherwise ungoverned client must cross principal
            # projection so an active principal cannot discover untagged tools.
            untagged_client = not client_requirements and not tool_permissions
            if (
                untagged_client
                and not permissions_are_available(())
            ) or (
                client_requirements
                and not permissions_are_available(client_requirements)
            ):
                return []
            from ...mcp_resources import discover_remote_tools

            native = super().get_tools
            tools = await discover_remote_tools(lambda: native(readonly_context), configured, "adk")
            names = tuple(
                str(getattr(tool, "name", ""))
                for tool in tools
            )
            _validate_mcp_permission_tools(tool_permissions, names, client_name)
            if policy is not None:
                _validate_approval_tools(policy, names, capability_id=client_name)
            selected: list[Any] = []
            for remote_tool in tools:
                # BaseToolset applies its presentation prefix only after
                # this method returns, so these are canonical remote names.
                name = str(getattr(remote_tool, "name", ""))
                requirements = tuple(
                    item
                    for item in (required_permission, tool_permissions.get(name))
                    if item is not None
                )
                attach_required_permissions(remote_tool, requirements)
                if policy is not None and policy.applies_to(name):
                    # ADK creates MCP tools only after discovery. Wrapping its
                    # public execution method is the only point that can audit
                    # success after transport I/O without copying MCP internals.
                    remote_tool.run_async = _guard_mcp_tool(
                        remote_tool.run_async,
                        client_name=client_name,
                        tool_name=name,
                        policy=policy,
                    )
                if capability_is_available(remote_tool):
                    selected.append(remote_tool)
            from ...mcp_capability_tools import model_capability_tools
            from ...mcp_resources import tool_capabilities

            local = self.get_context_tools(tools)
            capabilities = await tool_capabilities(
                tools, configured, "adk", session_manager=getattr(self, "_mcp_session_manager", None),
            )
            selected.extend(model_capability_tools(local, configured, capabilities))
            return selected

        def get_context_tools(self, existing: list[Any]) -> list[Any]:
            """Build developer operations independently of the model's capability projection."""

            from ...agent_principal import capability_is_available

            # Local helpers already carry canonical aliases. Exclude model
            # projections before checking collisions with remote names.
            remote = [tool for tool in existing if not getattr(tool, "__harnest_mcp_capability__", None)]
            return [tool for tool in adk_capability_tools(configured, remote) if capability_is_available(tool)]

    return GovernedMcpToolset


def _connection(
    client: Any,
    classes: tuple[Any, ...],
    *,
    binding: _MCPClientLifecycleBinding | None = None,
) -> Any:
    """Build ADK parameters and install the portable HTTP-client seam."""

    _, sse_params, stdio_params, stream_params, server_parameters = classes
    if client.transport == "stdio":
        return _stdio(client, stdio_params, server_parameters)
    params = sse_params if client.transport == "sse" else stream_params
    kwargs: dict[str, Any] = {
        "url": client._expand(client.url or ""),
        "headers": {
            key: client._expand(value) for key, value in client.headers.items()
        },
        "timeout": client.timeout_seconds,
        "sse_read_timeout": client.sse_read_timeout_seconds,
    }
    if binding is not None:
        # Both framework adapters own and close each returned session client.
        kwargs["httpx_client_factory"] = binding.client_factory()
    if client.portable is not None:
        from ...agent_plugin_runtime import portable_http_factory
        kwargs["httpx_client_factory"] = portable_http_factory(client.url or "")
    return params(**kwargs)

def _stdio(client: Any, params: Any, server_parameters: Any) -> Any:
    """Share stdio configuration without host expansion of portable fields."""
    configuration = client._stdio_configuration()
    configuration["env"] = configuration["env"] or None
    server = server_parameters(**configuration)
    return params(server_params=server, timeout=client.timeout_seconds)


def _guard_mcp_tool(
    operation: Any,
    *,
    client_name: str,
    tool_name: str,
    policy: ApprovalPolicy,
) -> Any:
    async def guarded(*, args: dict[str, Any], tool_context: Any) -> Any:
        return await _invoke_governed_mcp_call(
            lambda: operation(args=args, tool_context=tool_context),
            client_name=client_name,
            tool_name=tool_name,
            arguments=args,
            policy=policy,
        )

    return guarded

def toolset_metadata(toolset: Any) -> tuple[str, str, bool]:
    """Return only the adapter facts needed to bind invocation-scoped tools."""

    public_name = getattr(toolset, _ADK_PUBLIC_NAME, None)
    client_name = getattr(toolset, _ADK_CLIENT_NAME, None)
    approval_wrapped = getattr(toolset, _ADK_APPROVAL_WRAPPED, None)
    if not isinstance(public_name, str) or not public_name:
        raise TypeError("ADK MCP toolset is missing Harnest public-name metadata")
    if not isinstance(client_name, str) or not client_name:
        raise TypeError("ADK MCP toolset is missing Harnest capability metadata")
    if not isinstance(approval_wrapped, bool):
        raise TypeError("ADK MCP toolset is missing Harnest governance metadata")
    return public_name, client_name, approval_wrapped

def _attach_toolset_metadata(
    toolset: Any,
    *,
    public_name: str,
    client_name: str,
    approval_wrapped: bool,
) -> None:
    """Attach stable adapter facts without retaining the connection declaration."""

    # Some ADK releases use Pydantic models that reject undeclared setattr even
    # though the runtime object can safely carry private integration metadata.
    object.__setattr__(toolset, _ADK_PUBLIC_NAME, public_name)
    object.__setattr__(toolset, _ADK_CLIENT_NAME, client_name)
    object.__setattr__(toolset, _ADK_APPROVAL_WRAPPED, approval_wrapped)

def _mcp_classes() -> tuple[Any, ...]:
    """Load ADK MCP types without leaking Harnest-owned feature warnings."""

    from ._warnings import suppress_adk_warnings

    try:
        # Importing ADK MCP support instantiates its experimental auth registry.
        # Harnest owns that choice, so the upstream warning is not user-actionable.
        with suppress_adk_warnings("pluggable_auth"):
            from google.adk.tools.mcp_tool import (
                McpToolset, SseConnectionParams, StdioConnectionParams,
                StreamableHTTPConnectionParams,
            )
            from mcp import StdioServerParameters
    except ImportError:  # pragma: no cover - depends on optional runtime
        return _fallback_mcp_classes()
    return (McpToolset, SseConnectionParams, StdioConnectionParams,
            StreamableHTTPConnectionParams, StdioServerParameters)

def _fallback_mcp_classes() -> tuple[Any, ...]:
    """Load ADK MCP types from their defining compatibility modules."""

    from ._warnings import suppress_adk_warnings

    # ADK releases differ in what mcp_tool re-exports, so use the defining
    # modules as a compatibility seam for the supported 2.x line.
    try:
        with suppress_adk_warnings("pluggable_auth"):
            from google.adk.tools.mcp_tool.mcp_session_manager import (
                SseConnectionParams, StdioConnectionParams,
                StreamableHTTPConnectionParams,
            )
            from mcp import StdioServerParameters
            try:
                from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
            except ImportError:
                from google.adk.tools.mcp_tool import McpToolset
    except ImportError as exc:  # pragma: no cover - optional runtime
        raise RuntimeError(
            "google-adk with MCP support is required to use MCPClient"
        ) from exc
    return (McpToolset, SseConnectionParams, StdioConnectionParams,
            StreamableHTTPConnectionParams, StdioServerParameters)


def adk_capability_tools(configured: Any, existing: list[Any]) -> list[Any]:
    """Add local capability operations while rejecting remote reserved-name collisions."""

    from google.adk.tools.function_tool import FunctionTool
    from ...agent_principal import attach_required_permissions

    functions = capability_functions(configured, framework="adk")
    # Explicit ADK prefixes are applied by BaseToolset later. Otherwise namespace
    # our local helpers without changing the author's existing remote tool names.
    prefix = "" if configured.tool_name_prefix else f"{configured._client_name()}_"
    _check_names(existing, functions, prefix=prefix)
    tools = []
    for operation in functions:
        canonical = operation.__name__
        # ADK builds declarations from the callable, not tool.name. Each
        # operation is connection-local, so naming it here keeps both aligned.
        operation.__name__ = prefix + canonical
        tool = FunctionTool(func=operation)
        setattr(tool, "__harnest_mcp_capability__", canonical)
        attach_required_permissions(tool, () if configured.permission is None else (configured.permission,))
        tools.append(tool)
    return tools


def portable_adk_toolset(base: type[BaseToolset], client: MCPClient) -> type[BaseToolset]:
    """Make unavailable portable servers nonfatal to ADK's independent tools."""
    class PortableToolset(base):
        _portable_disabled = False

        async def get_tools(self, readonly_context: ReadonlyContext | None=None) -> list[BaseTool]:
            """Defer writable state until discovery and contain connection failures."""
            if self._portable_disabled:
                return []
            try:
                if client.transport == "stdio":
                    client.portable.prepare()
                return await super().get_tools(readonly_context)
            except Exception as error:
                self._portable_disabled = True
                client.portable.failed(error)
                return []

    return PortableToolset

def disabled_adk_toolset() -> BaseToolset:
    """Preserve native toolset lifecycle when a portable server cannot be configured."""
    from google.adk.tools.base_toolset import BaseToolset

    class DisabledToolset(BaseToolset):
        async def get_tools(self, readonly_context: ReadonlyContext | None=None) -> list[BaseTool]:
            """Expose no tools without retrying invalid configuration each turn."""
            return []

        async def close(self) -> None:
            """No connection was opened, so there is no provider state to release."""

    return DisabledToolset()
