"""Declarative MCP client connections for the ADK and LangGraph backends."""

from __future__ import annotations




import os
import re
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from dataclasses import dataclass, field
from datetime import timedelta
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Literal, Mapping, Sequence, cast

from .frameworks.base import FrameworkName
from .agent.approval import (
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
)
from .mcp_context import (
    MCPClientUnavailableError, MCPContext, MCPContextUnavailableError,
    MCPLifecycleError, MCPLifecyclePipeline, MCPToolCallError, MCPToolCallRequest,
    MCPToolLifecycleContext, MCPToolUnavailableError, ManagedMCPClient,
)
from .mcp_resources import MCPResourceClient, MCPResourceError
from .mcp_subscriptions import MCPResourceEvent, MCPSubscription, SubscriptionLifecycle

_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")

__all__ = [
    "MCPClientUnavailableError", "MCPContext", "MCPContextUnavailableError",
    "MCPLifecycleError", "MCPLifecyclePipeline", "MCPToolCallError", "MCPToolCallRequest",
    "MCPToolLifecycleContext", "MCPToolUnavailableError", "ManagedMCPClient",
    "MCPClient",
    "MCPClientContext",
    "MCPClientLifecycle",
    "MCPHTTPClientOptions",
    "MCPResourceClient", "MCPResourceError",
    "MCPResourceEvent", "MCPSubscription",
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
    permission: str | None = None
    tool_permissions: Mapping[str, str] = field(default_factory=dict, repr=False)
    resources: Sequence[str] | None = None
    prompts: Sequence[str] | None = None
    max_content_bytes: int = 1024 * 1024
    subscriptions: Sequence[MCPSubscription] = field(default_factory=tuple, repr=False)
    _subscription_controller: _MCPClientLifecycleController | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _lifecycle_controller: _MCPClientLifecycleController | None = field(
        default=None, init=False, repr=False, compare=False
    )

    @classmethod
    def from_openapi(
        cls, source: str | Path, *, base_url: str | None = None,
        headers: Mapping[str, str] | None = None, **client_options: Any,
    ) -> "MCPClient":
        """Connect to a local or remote spec through a runtime OpenAPI bridge.

        Reading and conversion happen when the MCP connection opens, never
        during authoring or compilation. Use a path relative to ``__file__``
        for specs bundled with an agent. Headers support ``${VARIABLE}``.
        ``client_options`` accepts the standard ``stdio`` policy options.
        Requires the optional ``harnest[openapi]`` runtime dependency.
        """

        import sys
        from .openapi_bridge import connection_environment

        environment = connection_environment(source, base_url, headers)
        return cls.stdio(sys.executable, "-m", "harnest.openapi_bridge",
                         env=environment, **client_options)

    @classmethod
    def stdio(
        cls,
        command: str,
        *args: str,
        env: Mapping[str, str] | None = None,
        tools: Sequence[str] | None = None,
        prefix: str | None = None,
        permission: str | None = None,
        tool_permissions: Mapping[str, str] | None = None,
        timeout_seconds: float = 30,
        lifecycle: MCPClientLifecycle | None = None,
        resources: Sequence[str] | None = None,
        prompts: Sequence[str] | None = None,
        max_content_bytes: int = 1024 * 1024,
        subscriptions: Sequence[MCPSubscription] = (),
    ) -> "MCPClient":
        """Create an MCP client that starts and communicates with a subprocess."""

        return cls(
            "stdio",
            command=command,
            args=args,
            resources=resources, prompts=prompts, max_content_bytes=max_content_bytes,
            subscriptions=subscriptions,
            env=env or {},
            tool_filter=tools,
            tool_name_prefix=prefix,
            permission=permission,
            tool_permissions=tool_permissions or {},
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
        permission: str | None = None,
        tool_permissions: Mapping[str, str] | None = None,
        timeout_seconds: float = 30,
        sse_read_timeout_seconds: float = 300,
        lifecycle: MCPClientLifecycle | None = None,
        resources: Sequence[str] | None = None,
        prompts: Sequence[str] | None = None,
        max_content_bytes: int = 1024 * 1024,
        subscriptions: Sequence[MCPSubscription] = (),
    ) -> "MCPClient":
        """Create an MCP client using the Streamable HTTP transport."""

        return cls(
            "streamable-http",
            resources=resources, prompts=prompts, max_content_bytes=max_content_bytes,
            subscriptions=subscriptions,
            url=url,
            headers=headers or {},
            tool_filter=tools,
            tool_name_prefix=prefix,
            permission=permission,
            tool_permissions=tool_permissions or {},
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
        permission: str | None = None,
        tool_permissions: Mapping[str, str] | None = None,
        timeout_seconds: float = 30,
        sse_read_timeout_seconds: float = 300,
        lifecycle: MCPClientLifecycle | None = None,
        resources: Sequence[str] | None = None,
        prompts: Sequence[str] | None = None,
        max_content_bytes: int = 1024 * 1024,
        subscriptions: Sequence[MCPSubscription] = (),
    ) -> "MCPClient":
        """Create an MCP client using the legacy HTTP/SSE transport."""

        return cls(
            "sse",
            resources=resources, prompts=prompts, max_content_bytes=max_content_bytes,
            subscriptions=subscriptions,
            url=url,
            headers=headers or {},
            tool_filter=tools,
            tool_name_prefix=prefix,
            permission=permission,
            tool_permissions=tool_permissions or {},
            timeout_seconds=timeout_seconds,
            sse_read_timeout_seconds=sse_read_timeout_seconds,
            lifecycle=lifecycle,
        )

    def __post_init__(self) -> None:
        """Validate authoring policy without opening connections or running handlers."""

        self._validate_resource_policy()
        self._validate_timeouts()
        self._validate_transport()
        self._validate_permissions()
        self._initialize_lifecycle()
        self._initialize_subscriptions()

    def _initialize_subscriptions(self) -> None:
        """Bind opt-in listeners separately so inspection cannot start application work."""

        subscriptions = tuple(self.subscriptions)
        for subscription in subscriptions:
            if not isinstance(subscription, MCPSubscription):
                raise TypeError("MCP subscriptions must contain MCPSubscription values")
            if subscription.method == "subscriptions/listen" and self.transport != "streamable-http":
                raise ValueError("subscriptions/listen currently requires Streamable HTTP")
            if self.resources is not None and subscription.uri not in self.resources:
                raise ValueError("MCP subscription URI must be allowed by resources")
        if len({item.uri for item in subscriptions}) != len(subscriptions):
            raise ValueError("MCP subscription URIs must be unique per client")
        object.__setattr__(self, "subscriptions", subscriptions)
        if subscriptions:
            controller = _mcp_lifecycle_controller(SubscriptionLifecycle(self))
            object.__setattr__(self, "_subscription_controller", controller)

    def _runtime_bindings(self, framework: str) -> tuple[Any, ...]:
        """Start credentials before listeners and close listeners before credentials."""

        bindings = []
        binding = self._lifecycle_binding(framework)
        if binding is not None:
            bindings.append(binding)
        if self._subscription_controller is not None:
            context = MCPClientContext(
                name=self._client_name(), transport=self.transport, framework=framework,
                url=self._expand(self.url or ""),
            )
            bindings.append(_MCPClientLifecycleBinding(self._subscription_controller, context))
        return tuple(bindings)

    def _validate_resource_policy(self) -> None:
        """Freeze explicit filters and reject ambiguous content limits before connection."""

        if type(self.max_content_bytes) is not int or not 1024 <= self.max_content_bytes <= 16 * 1024 * 1024:
            raise ValueError("max_content_bytes must be between 1024 and 16777216")
        for name in ("resources", "prompts"):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, (str, bytes)) or any(not isinstance(item, str) or not item.strip() for item in value):
                raise TypeError(f"MCP {name} must be a sequence of non-empty identifiers")
            object.__setattr__(self, name, tuple(value))

    def connect(self) -> AbstractAsyncContextManager[MCPResourceClient]:
        """Open a read client using Harnest's selected framework and credentials."""

        from .frameworks.context import current_framework
        from .mcp_resources import managed_resource_client

        return managed_resource_client(self, framework=current_framework().name)

    def _validate_permissions(self) -> None:
        """Freeze client and tool requirements using permissioned-tool identifiers."""

        from .agent_principal import validate_permission

        if self.permission is not None:
            validate_permission(self.permission)
        if not isinstance(self.tool_permissions, Mapping):
            raise TypeError("MCP tool_permissions must be a mapping")
        normalized: dict[str, str] = {}
        for name, permission in self.tool_permissions.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("MCP permission tool names must be non-empty strings")
            normalized[name] = validate_permission(permission)
        object.__setattr__(
            self, "tool_permissions", MappingProxyType(normalized)
        )

    def required_permissions_for(self, tool_name: str) -> frozenset[str]:
        """Combine an optional client requirement with one remote-tool requirement."""

        return frozenset(
            item
            for item in (self.permission, self.tool_permissions.get(tool_name))
            if item is not None
        )

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

    def _client_name(self) -> str:
        """Return the canonical capability identity used by approval and audit."""

        return self.capability_id or self.identity or self.tool_name_prefix or "mcp"

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

    def connection_options(self) -> dict[str, Any]:
        """Return this server's connection options without opening a session."""

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
        self, framework: FrameworkName
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
        client.resources,
        client.prompts,
        client.max_content_bytes,
        tuple(client.subscriptions),
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


def _validate_mcp_permission_tools(
    tool_permissions: Mapping[str, str],
    remote_names: Sequence[str],
    capability_id: str,
) -> None:
    """Fail closed when permission metadata names an unavailable remote tool."""

    missing = sorted(set(tool_permissions) - set(remote_names))
    if missing:
        raise ValueError(
            f"MCP capability {capability_id!r} permissions reference unavailable "
            f"tools: {', '.join(missing)}"
        )
