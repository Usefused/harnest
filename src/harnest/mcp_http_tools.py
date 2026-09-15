"""Project modern HTTP MCP tools through existing framework governance boundaries."""

from typing import Any

from .mcp_http_session import modern_session
from .mcp_http_transport import _call, _client
from .mcp_resources import MCPResourceError


async def modern_tools(configured: Any, framework: str, *, server_name: str | None = None, interceptors: Any = None) -> list[Any] | None:
    """Discover modern schemas before asking framework adapters to initialize."""

    from .mcp_http_tool_headers import valid_tool_schema

    async with modern_session(configured, framework) as modern:
        if modern is None:
            return None
        session, initialized = modern
        if initialized.capabilities.tools is None:
            return []
        schemas = await _tool_pages(session, configured.max_content_bytes)
    schemas = [tool for tool in schemas if valid_tool_schema(tool.inputSchema)]
    if configured.tool_filter is not None:
        schemas = [tool for tool in schemas if tool.name in configured.tool_filter]
    return _framework_tools(configured, framework, schemas, server_name, interceptors)


def _framework_tools(configured: Any, framework: str, schemas: list[Any], server_name: str | None, interceptors: Any) -> list[Any]:
    """Keep framework projection separate from protocol and catalogue selection."""

    calls = ToolCalls(configured, framework, schemas)
    if framework == "adk":
        return [_adk_tool(schema, calls) for schema in schemas]
    from langchain_mcp_adapters.tools import convert_mcp_tool_to_langchain_tool

    return [convert_mcp_tool_to_langchain_tool(
        calls, schema, server_name=server_name, tool_name_prefix=True,
        tool_interceptors=interceptors,
    ) for schema in schemas]


async def _tool_pages(session: Any, limit: int) -> list[Any]:
    """Bound total discovery and reject cyclic pagination or ambiguous tool names."""

    tools, cursors, names = [], set(), set()
    cursor, size = None, 0
    while True:
        page = await session.list_tools(cursor=cursor)
        size += len(page.model_dump_json().encode())
        if size > limit:
            raise MCPResourceError("MCP tool discovery exceeds max_content_bytes")
        for tool in page.tools:
            if tool.name in names:
                raise MCPResourceError("MCP server exposes duplicate tool names")
            names.add(tool.name)
            tools.append(tool)
        cursor = page.nextCursor
        if cursor is None:
            return tools
        if cursor in cursors:
            raise MCPResourceError("MCP tool discovery returned a repeated cursor")
        cursors.add(cursor)


class ToolCalls:
    """Reconnect stateless requests using current credentials, without protocol fallback."""

    def __init__(self, configured: Any, framework: str, schemas: list[Any]) -> None:
        self.configured, self.framework = configured, framework
        self.schemas = {tool.name: tool.inputSchema for tool in schemas}

    async def call_tool(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        """Invoke once; transport and input-required errors cannot trigger replay."""

        from mcp.types import CallToolResult
        from .mcp_http_tool_headers import tool_headers
        from .mcp_resources import _resource_failure

        if name not in self.schemas:
            raise MCPResourceError("MCP tool was not discovered")
        try:
            headers = tool_headers(self.schemas[name], arguments)
            async with _client(self.configured, self.framework) as (client, url):
                result = await _call(client, url, "tools/call", {"name": name, "arguments": arguments},
                                     self.configured.max_content_bytes, headers=headers)
                return CallToolResult.model_validate(result)
        except Exception as error:
            failure = _resource_failure(error, "tools/call")
        raise failure from None


def _adk_tool(schema: Any, calls: ToolCalls) -> Any:
    """Reuse ADK schema conversion while replacing only its initialized transport."""

    from google.adk.tools.mcp_tool.mcp_tool import McpTool

    class HTTPTool(McpTool):
        """Governance remains in the enclosing toolset and invocation facade."""

        async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:
            """Return MCP content without opening a 2025 session."""

            result = await calls.call_tool(schema.name, args)
            return result.model_dump(mode="json", by_alias=True, exclude_none=True)

    return HTTPTool(mcp_tool=schema, mcp_session_manager=None)
