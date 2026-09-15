"""Bounded MCP discovery, resources, and prompts shared by CLI and agent tools."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
import json
from typing import Any, Mapping


class MCPResourceError(RuntimeError):
    """An MCP capability request failed without exposing connection credentials."""


@asynccontextmanager
async def managed_resource_client(configured: Any, *, framework: str = "langgraph"):
    """Acquire only this caller's lifecycle ownership and release it on every exit."""

    binding = configured._lifecycle_binding(framework)
    owned = await binding.start() if binding is not None else False
    client = MCPResourceClient(configured, framework=framework)
    try:
        yield client
    finally:
        client._closed = True
        if owned:
            await binding.close(reset=True)


def _bounded_result(value: Any, limit: int) -> dict[str, Any]:
    """Normalize SDK values while bounding what reaches a caller or model."""

    result = value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if len(json.dumps(result).encode()) > limit:
        raise MCPResourceError("MCP response exceeds max_content_bytes")
    return result


@asynccontextmanager
async def resource_session(configured: Any, framework: str = "langgraph", *, message_handler: Any = None):
    """Share discovery selection across CLI, playground, and model-facing reads."""

    from .mcp_http_session import modern_session

    # An explicit SDK subscription retains its requested wire semantics.
    if message_handler is None:
        async with modern_session(configured, framework) as modern:
            if modern is not None:
                yield modern
                return
    async with sdk_resource_session(configured, framework, message_handler=message_handler) as legacy:
        yield legacy


@asynccontextmanager
async def sdk_resource_session(configured: Any, framework: str = "langgraph", *, message_handler: Any = None):
    """Own the SDK 1.x transport in one task for initialized-protocol servers."""

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamablehttp_client

    if configured.portable is not None:
        configured.portable.prepare()
    if configured.transport == "stdio":
        transport = stdio_client(StdioServerParameters(**configured._stdio_configuration()))
    else:
        options = configured.to_langgraph_connection()
        options.pop("transport")
        options.pop("session_kwargs", None)
        binding = configured._lifecycle_binding(framework)
        if binding is not None:
            options["httpx_client_factory"] = binding.client_factory()
        transport_type = sse_client if configured.transport == "sse" else streamablehttp_client
        transport = transport_type(**options)
    async with transport as streams:
        async with ClientSession(
            streams[0], streams[1],
            read_timeout_seconds=timedelta(seconds=configured.timeout_seconds),
            message_handler=message_handler,
        ) as session:
            initialized = await session.initialize()
            yield session, initialized


class MCPResourceClient:
    """Connection-bound read API; resource data never becomes system instructions."""

    def __init__(self, configured: Any, *, framework: str = "langgraph") -> None:
        self._configured = configured
        self._framework = framework
        self._closed = False

    async def inspect(self) -> dict[str, Any]:
        """Discover capability metadata without reading resources or rendering prompts."""

        return await self._execute("inspect", {})

    async def list_tools(self, cursor: str | None = None) -> dict[str, Any]:
        """List one page of remote tool schemas under the configured tool policy."""

        return await self._execute("list_tools", {"cursor": cursor})

    async def list_resources(self, cursor: str | None = None) -> dict[str, Any]:
        """Return one bounded page with the server's opaque nextCursor unchanged."""

        return await self._execute("list_resources", {"cursor": cursor})

    async def list_resource_templates(self, cursor: str | None = None) -> dict[str, Any]:
        """Discover parameterized resource URIs without fetching their contents."""

        return await self._execute("list_resource_templates", {"cursor": cursor})

    async def list_prompts(self, cursor: str | None = None) -> dict[str, Any]:
        """Discover prompt descriptions and declared arguments."""

        return await self._execute("list_prompts", {"cursor": cursor})

    async def read_resource(self, uri: str) -> dict[str, Any]:
        """Read an explicitly selected URI through MCP, never through a local file API."""

        self._require_allowed("resources", uri)
        return await self._execute("read_resource", {"uri": uri})

    async def get_prompt(self, name: str, arguments: Mapping[str, str] | None = None) -> dict[str, Any]:
        """Retrieve prompt messages as untrusted data for explicit caller use."""

        self._require_allowed("prompts", name)
        if arguments is not None and (not isinstance(arguments, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in arguments.items()
        )):
            raise TypeError("MCP prompt arguments must map strings to strings")
        return await self._execute("get_prompt", {"name": name, "arguments": dict(arguments or {})})

    def _require_allowed(self, kind: str, value: str) -> None:
        """Enforce exact configured selections before opening a remote connection."""

        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"MCP {kind} identifier must be non-empty")
        allowed = getattr(self._configured, kind)
        if allowed is not None and value not in allowed:
            raise MCPResourceError(f"MCP {kind} identifier is not allowed")

    async def _execute(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Contain provider errors and close short-lived sessions without masking cancellation."""

        if self._closed:
            raise MCPResourceError("MCP read client is closed")
        try:
            async with resource_session(self._configured, self._framework) as (session, initialized):
                if operation == "inspect":
                    return await self._inspect(session, initialized)
                result = await self._request(session, initialized.capabilities, operation, arguments)
                return self._filter(result)
        except MCPResourceError:
            raise
        except Exception as error:
            failure = _resource_failure(error, operation)
        # SDK errors can contain endpoint URLs, tokens, or server-controlled text.
        raise failure from None

    async def _request(self, session: Any, capabilities: Any, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Avoid unsupported optional requests and preserve paginated results."""

        kind = _capability_kind(operation)
        if getattr(capabilities, kind, None) is None:
            empty = {"list_tools": "tools", "list_resources": "resources", "list_resource_templates": "resourceTemplates", "list_prompts": "prompts"}
            if operation in empty:
                return {empty[operation]: []}
            raise MCPResourceError(f"MCP server does not advertise {kind}")
        try:
            result = await getattr(session, operation)(**arguments)
        except Exception as error:
            # A resource server need not expose parameterized URI templates.
            if operation != "list_resource_templates" or not _missing_method(error):
                raise
            return {"resourceTemplates": []}
        return _bounded_result(result, self._configured.max_content_bytes)

    async def _inspect(self, session: Any, initialized: Any) -> dict[str, Any]:
        """Return one page of each catalogue, exposing cursors instead of unbounded discovery."""

        result = {
            "serverInfo": initialized.serverInfo.model_dump(mode="json"),
            "protocolVersion": initialized.protocolVersion,
            "capabilities": initialized.capabilities.model_dump(mode="json", exclude_none=True),
        }
        for operation in ("list_tools", "list_resources", "list_resource_templates", "list_prompts"):
            result[operation.removeprefix("list_")] = self._filter(
                await self._request(session, initialized.capabilities, operation, {})
            )
        if len(json.dumps(result).encode()) > self._configured.max_content_bytes:
            raise MCPResourceError("MCP discovery exceeds max_content_bytes")
        return result

    def _tool_visible(self, name: str) -> bool:
        """Keep discovery metadata inside the same principal policy as tool execution."""

        from .agent_principal import permissions_are_available

        requirements = tuple(value for value in (
            self._configured.permission, self._configured.tool_permissions.get(name),
        ) if value is not None)
        return permissions_are_available(requirements)

    def _filter(self, result: dict[str, Any]) -> dict[str, Any]:
        """Project catalogues through exact allowlists without changing server cursors."""

        if "tools" in result:
            result["tools"] = self._filter_tools(result["tools"])
        for field, kind, key in (("resources", "resources", "uri"), ("resourceTemplates", "resources", "uriTemplate"), ("prompts", "prompts", "name")):
            allowed = getattr(self._configured, kind)
            if field in result and allowed is not None:
                result[field] = [item for item in result[field] if item[key] in allowed]
        return result

    def _filter_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Apply both explicit tool selection and invocation permission projection."""

        allowed = self._configured.tool_filter
        selected = tools if allowed is None else [item for item in tools if item["name"] in allowed]
        return [item for item in selected if self._tool_visible(item["name"])]


def _capability_kind(operation: str) -> str:
    """Select the advertised optional capability for one protocol operation."""

    if operation == "list_tools":
        return "tools"
    return "prompts" if "prompt" in operation else "resources"


async def discover_remote_tools(operation: Any, configured: Any, framework: str, **options: Any) -> list[Any]:
    """Select the same protocol for native tools and context resource discovery."""

    from .mcp_http_tools import modern_tools

    try:
        modern = await modern_tools(configured, framework, **options)
    except Exception as error:
        if configured.portable is not None:
            configured.portable.failed(error)
            return []
        failure = _resource_failure(error, "tool discovery")
        raise failure from None
    if modern is not None:
        return modern

    try:
        return await operation()
    except Exception as error:
        if not _missing_method(error):
            raise
        async with sdk_resource_session(configured, framework) as (_, initialized):
            if initialized.capabilities.tools is not None:
                raise error
        return []


def _missing_method(error: Exception, depth: int = 0) -> bool:
    """Recognize only the protocol's method-not-found error, including AnyIO wrapping."""

    from mcp.shared.exceptions import McpError
    from .mcp_http_transport import ProtocolError

    if depth > 8:
        return False
    if isinstance(error, McpError):
        return error.error.code == -32601
    if isinstance(error, ProtocolError):
        return error.code == -32601
    if error.__cause__ is not None:
        return _missing_method(error.__cause__, depth + 1)
    children = getattr(error, "exceptions", ())
    return bool(children) and all(_missing_method(child, depth + 1) for child in children)


def _resource_failure(error: Exception, operation: str) -> MCPResourceError:
    """Retain Harnest's own safe validation errors wrapped by AnyIO task groups."""

    if isinstance(error, MCPResourceError):
        return MCPResourceError(str(error))
    children = getattr(error, "exceptions", ())
    for child in children:
        failure = _resource_failure(child, operation)
        if str(failure) != f"MCP {operation} failed":
            return failure
    return MCPResourceError(f"MCP {operation} failed")
