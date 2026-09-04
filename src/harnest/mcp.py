"""Declarative MCP client connections for the ADK and LangGraph backends."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Awaitable, Callable, Literal, Mapping, Sequence, cast

from .approval import (
    ApprovalPolicy,
    authorize_mcp,
    record_approved_execution,
    record_approved_failure,
)
from .mcp_lifecycle import (
    MCPClientContext,
    MCPClientLifecycle,
    MCPHTTPClientOptions,
    _MCPClientLifecycleBinding,
    _MCPClientLifecycleController,
    _mcp_lifecycle_controller,
    attach_mcp_lifecycle,
)
from .mcp_context import (
    MCPClientUnavailableError, MCPContext, MCPContextUnavailableError,
    MCPLifecycleError, MCPLifecyclePipeline, MCPToolCallError, MCPToolCallRequest,
    MCPToolLifecycleContext, MCPToolUnavailableError, ManagedMCPClient,
)

_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
_ADK_PUBLIC_NAME = "__harnest_mcp_public_name__"
_ADK_CLIENT_NAME = "__harnest_mcp_client_name__"
_ADK_APPROVAL_WRAPPED = "__harnest_mcp_approval_wrapped__"

__all__ = [
    "MCPClientUnavailableError", "MCPContext", "MCPContextUnavailableError",
    "MCPLifecycleError", "MCPLifecyclePipeline", "MCPToolCallError", "MCPToolCallRequest",
    "MCPToolLifecycleContext", "MCPToolUnavailableError", "ManagedMCPClient",
    "MCPClient",
    "MCPClientContext",
    "MCPClientLifecycle",
    "MCPHTTPClientOptions",
]


def _expand(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        try:
            return os.environ[name]
        except KeyError as exc:
            raise ValueError(
                f"MCP configuration references unset environment variable {name}"
            ) from exc

    return _ENV_PATTERN.sub(replace, value)


@dataclass(frozen=True, slots=True)
class MCPClient:
    """A client connection to a stdio, SSE, or Streamable HTTP MCP server."""

    transport: str
    command: str | None = None
    args: Sequence[str] = field(default_factory=tuple)
    env: Mapping[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    tool_filter: Sequence[str] | None = None
    tool_name_prefix: str | None = None
    timeout_seconds: float = 30
    sse_read_timeout_seconds: float = 300
    approval: ApprovalPolicy | None = field(default=None, repr=False)
    identity: str | None = field(default=None, repr=False)
    capability_id: str | None = field(default=None, repr=False)
    lifecycle: MCPClientLifecycle | None = field(default=None, repr=False)
    cwd: str | None = None
    portable: Any = field(default=None, repr=False)
    _lifecycle_controller: _MCPClientLifecycleController | None = field(
        default=None, init=False, repr=False, compare=False
    )

    @classmethod
    def stdio(
        cls,
        command: str,
        *args: str,
        env: Mapping[str, str] | None = None,
        tools: Sequence[str] | None = None,
        prefix: str | None = None,
        timeout_seconds: float = 30,
        lifecycle: MCPClientLifecycle | None = None,
    ) -> "MCPClient":
        return cls(
            "stdio",
            command=command,
            args=args,
            env=env or {},
            tool_filter=tools,
            tool_name_prefix=prefix,
            timeout_seconds=timeout_seconds,
            lifecycle=lifecycle,
        )

    @classmethod
    def streamable_http(
        cls,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        tools: Sequence[str] | None = None,
        prefix: str | None = None,
        timeout_seconds: float = 30,
        sse_read_timeout_seconds: float = 300,
        lifecycle: MCPClientLifecycle | None = None,
    ) -> "MCPClient":
        return cls(
            "streamable-http",
            url=url,
            headers=headers or {},
            tool_filter=tools,
            tool_name_prefix=prefix,
            timeout_seconds=timeout_seconds,
            sse_read_timeout_seconds=sse_read_timeout_seconds,
            lifecycle=lifecycle,
        )

    @classmethod
    def sse(
        cls,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        tools: Sequence[str] | None = None,
        prefix: str | None = None,
        timeout_seconds: float = 30,
        sse_read_timeout_seconds: float = 300,
        lifecycle: MCPClientLifecycle | None = None,
    ) -> "MCPClient":
        return cls(
            "sse",
            url=url,
            headers=headers or {},
            tool_filter=tools,
            tool_name_prefix=prefix,
            timeout_seconds=timeout_seconds,
            sse_read_timeout_seconds=sse_read_timeout_seconds,
            lifecycle=lifecycle,
        )

    def __post_init__(self) -> None:
        self._validate_timeouts()
        self._validate_transport()
        self._initialize_lifecycle()

    def _initialize_lifecycle(self) -> None:
        """Validate and privately bind the optional remote-client lifecycle."""

        if self.lifecycle is None:
            return
        if not isinstance(self.lifecycle, MCPClientLifecycle):
            raise TypeError("MCP lifecycle must extend MCPClientLifecycle")
        if self.transport == "stdio":
            raise ValueError("MCP client lifecycle requires an HTTP transport")
        object.__setattr__(
            self,
            "_lifecycle_controller",
            _mcp_lifecycle_controller(self.lifecycle),
        )

    def _validate_timeouts(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("MCP timeout_seconds must be greater than zero")
        if self.sse_read_timeout_seconds <= 0:
            raise ValueError(
                "MCP sse_read_timeout_seconds must be greater than zero"
            )
        if isinstance(self.args, (str, bytes)):
            raise TypeError("MCP args must be a sequence of arguments, not a string")
        if isinstance(self.tool_filter, (str, bytes)):
            raise TypeError("MCP tools must be a sequence of tool names, not a string")

    def _validate_transport(self) -> None:
        if self.transport == "stdio" and (not self.command or not self.command.strip()):
            raise ValueError("stdio MCP clients require a server command")
        if self.transport in {"sse", "streamable-http"} and (
            not self.url or not self.url.strip()
        ):
            raise ValueError(f"{self.transport} MCP clients require a server URL")
        if self.transport not in {"stdio", "sse", "streamable-http"}:
            raise ValueError(f"unsupported MCP transport: {self.transport}")

    def to_adk_toolset(self):
        """Use native transport while preserving portable expansion and failure rules."""

        classes = _adk_mcp_classes()
        binding = self._lifecycle_binding("adk")
        toolset = self._construct_adk_toolset(classes, binding)
        _attach_adk_mcp_toolset_metadata(
            toolset,
            public_name=self.identity or self.tool_name_prefix or self._client_name(),
            client_name=self._client_name(),
            approval_wrapped=self.approval is not None,
        )
        if binding is not None:
            attach_mcp_lifecycle(toolset, binding)
        return toolset

    def _construct_adk_toolset(self, classes, binding):
        """Keep portable parameter and constructor failures inside their component."""
        from .agent_plugin_runtime import disabled_adk_toolset, portable_adk_toolset
        try:
            connection = self._adk_connection(classes, binding=binding)
            toolset_type = self._adk_toolset_type(classes[0])
            if self.portable is not None:
                toolset_type = portable_adk_toolset(toolset_type, self)
            return toolset_type(
                connection_params=connection,
                tool_filter=list(self.tool_filter) if self.tool_filter is not None else None,
                tool_name_prefix=self.tool_name_prefix,
            )
        except Exception as error:
            if self.portable is None:
                raise
            self.portable.failed(error)
            return disabled_adk_toolset()

    def _adk_toolset_type(self, base: Any) -> Any:
        policy = self.approval
        if policy is None:
            return base
        client_name = self._client_name()
        public_name = self.identity or self.tool_name_prefix or client_name
        exposed_prefix = f"{self.tool_name_prefix}_" if self.tool_name_prefix else ""

        class ApprovalMcpToolset(base):
            """Expose adapter identity and gate tools after ADK discovery."""

            # Invocation adapters need the canonical capability key without
            # retaining the MCPClient, whose fields include raw connection data.
            __harnest_mcp_public_name__ = public_name
            __harnest_mcp_client_name__ = client_name
            __harnest_mcp_approval_wrapped__ = True

            async def get_tools(self, readonly_context: Any = None) -> list[Any]:
                tools = await super().get_tools(readonly_context)
                names = tuple(
                    str(getattr(tool, "name", "")).removeprefix(exposed_prefix)
                    for tool in tools
                )
                _validate_approval_tools(policy, names, capability_id=client_name)
                for remote_tool in tools:
                    exposed_name = str(getattr(remote_tool, "name", ""))
                    name = exposed_name.removeprefix(exposed_prefix)
                    if not policy.applies_to(name):
                        continue

                    # ADK creates MCP tools only after discovery. Wrapping its
                    # public execution method is the only point that can audit
                    # success after transport I/O without copying MCP internals.
                    remote_tool.run_async = _guard_adk_mcp_tool(
                        remote_tool.run_async,
                        client_name=client_name,
                        tool_name=name,
                        policy=policy,
                    )
                return tools

        return ApprovalMcpToolset

    def _client_name(self) -> str:
        """Return the canonical capability identity used by approval and audit."""

        return self.capability_id or self.identity or self.tool_name_prefix or "mcp"

    def _adk_connection(
        self,
        classes: tuple[Any, ...],
        *,
        binding: _MCPClientLifecycleBinding | None = None,
    ) -> Any:
        """Build ADK parameters and install the portable HTTP-client seam."""

        _, sse_params, stdio_params, stream_params, server_parameters = classes
        if self.transport == "stdio":
            return self._adk_stdio(stdio_params, server_parameters)
        params = sse_params if self.transport == "sse" else stream_params
        kwargs: dict[str, Any] = {
            "url": self._expand(self.url or ""),
            "headers": {
                key: self._expand(value) for key, value in self.headers.items()
            },
            "timeout": self.timeout_seconds,
            "sse_read_timeout": self.sse_read_timeout_seconds,
        }
        if binding is not None:
            # Both framework adapters own and close each returned session client.
            kwargs["httpx_client_factory"] = binding.client_factory()
        if self.portable is not None:
            from .agent_plugin_runtime import portable_http_factory
            kwargs["httpx_client_factory"] = portable_http_factory(self.url or "")
        return params(**kwargs)

    def _adk_stdio(self, params: Any, server_parameters: Any) -> Any:
        """Share stdio configuration without host expansion of portable fields."""
        configuration = self._stdio_configuration()
        configuration["env"] = configuration["env"] or None
        server = server_parameters(**configuration)
        return params(server_params=server, timeout=self.timeout_seconds)

    def _expand(self, value: str) -> str:
        """Keep portable HTTP fields literal and native factory environment support."""
        return value if self.portable is not None else _expand(value)

    def _stdio_configuration(self) -> dict[str, Any]:
        """Keep one stdio mapping for both framework adapters."""
        if self.portable is not None:
            return self.portable.stdio(self)
        value = {"command": _expand(self.command or ""),
                 "args": [_expand(arg) for arg in self.args],
                 "env": {key: _expand(item) for key, item in self.env.items()}}
        if self.cwd is not None:
            value["cwd"] = _expand(self.cwd)
        return value

    def to_langgraph_connection(self) -> dict[str, Any]:
        """Return a LangChain MCP adapter connection without opening it."""

        if self.transport == "stdio":
            return {
                "transport": "stdio",
                **self._stdio_configuration(),
                "session_kwargs": {
                    "read_timeout_seconds": timedelta(seconds=self.timeout_seconds)
                },
            }
        connection: dict[str, Any] = {
            "transport": (
                "streamable_http"
                if self.transport == "streamable-http"
                else self.transport
            ),
            "url": self._expand(self.url or ""),
            "headers": {key: self._expand(value) for key, value in self.headers.items()},
            "timeout": self.timeout_seconds,
            "sse_read_timeout": self.sse_read_timeout_seconds,
            "session_kwargs": {
                "read_timeout_seconds": timedelta(seconds=self.timeout_seconds)
            },
        }
        binding = self._lifecycle_binding("langgraph")
        if binding is not None:
            # LangChain forwards this factory to the same MCP SDK transport seam.
            connection["httpx_client_factory"] = binding.client_factory()
        if self.portable is not None:
            from .agent_plugin_runtime import portable_http_factory
            connection["httpx_client_factory"] = portable_http_factory(self.url or "")
        return connection

    def _lifecycle_binding(
        self, framework: Literal["adk", "langgraph"]
    ) -> _MCPClientLifecycleBinding | None:
        """Resolve a privacy-safe framework binding for this connection."""

        controller = self._lifecycle_controller
        if controller is None:
            return None
        name = self._client_name()
        context = MCPClientContext(
            name=name,
            transport=cast(Literal["sse", "streamable-http"], self.transport),
            framework=framework,
            url=self._expand(self.url or ""),
        )
        return _MCPClientLifecycleBinding(controller, context)


def _guard_adk_mcp_tool(
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


def _adk_mcp_toolset_metadata(toolset: Any) -> tuple[str, str, bool]:
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


def _attach_adk_mcp_toolset_metadata(
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


async def _invoke_governed_mcp_call(
    operation: Callable[[], Awaitable[Any]],
    *,
    client_name: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    policy: ApprovalPolicy | None,
) -> Any:
    """Run a discovered MCP tool through the shared approval boundary.

    Runtime adapters and the invocation-scoped facade use one implementation so
    a context-originated call cannot accidentally receive weaker approval and
    execution accounting than the same framework tool called by the model.
    """

    grant = None
    if policy is not None:
        grant = await authorize_mcp(client_name, tool_name, arguments, policy)
    try:
        result = await operation()
    except BaseException:
        record_approved_failure(grant)
        raise
    if _mcp_result_failed(result):
        record_approved_failure(grant)
    else:
        record_approved_execution(grant)
    return result


def _mcp_result_failed(result: Any) -> bool:
    """Recognize portable MCP error results without inspecting their payload."""

    if isinstance(result, Mapping):
        return result.get("isError") is True or result.get("status") == "error"
    return getattr(result, "isError", False) is True or getattr(
        result, "status", None
    ) == "error"


def _mcp_connection_configuration(client: MCPClient) -> tuple[Any, ...]:
    """Return semantic connection fields without compiler policy metadata."""

    if not isinstance(client, MCPClient):
        raise TypeError("MCP connection configuration requires MCPClient")
    return (
        client.transport,
        client.command,
        tuple(client.args),
        tuple(sorted(client.env.items())),
        client.url,
        tuple(sorted(client.headers.items())),
        tuple(sorted(client.tool_filter)) if client.tool_filter is not None else None,
        client.tool_name_prefix,
        client.timeout_seconds,
        client.sse_read_timeout_seconds,
        client.cwd,
        client.portable,
        # Lifecycle instances may hold distinct certificate and gateway state.
        id(client.lifecycle) if client.lifecycle is not None else None,
    )


def _validate_approval_tools(
    policy: ApprovalPolicy | None,
    remote_names: Sequence[str],
    *,
    capability_id: str,
) -> None:
    """Fail closed when a selective policy names an unavailable remote tool."""

    if policy is None or policy.tools is None:
        return
    missing = sorted(set(policy.tools) - set(remote_names))
    if missing:
        raise ValueError(
            f"MCP capability {capability_id!r} approval references unavailable "
            f"tools: {', '.join(missing)}"
        )


def _adk_mcp_classes() -> tuple[Any, ...]:
    """Load ADK MCP types without leaking Harnest-owned feature warnings."""

    from ._adk_warnings import suppress_adk_warnings

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
        return _fallback_adk_mcp_classes()
    return (McpToolset, SseConnectionParams, StdioConnectionParams,
            StreamableHTTPConnectionParams, StdioServerParameters)


def _fallback_adk_mcp_classes() -> tuple[Any, ...]:
    """Load ADK MCP types from their defining compatibility modules."""

    from ._adk_warnings import suppress_adk_warnings

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
