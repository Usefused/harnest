"""Connect this agent to the configured authenticated MCP server."""

from harnest.mcp import MCPClient


def client() -> MCPClient:
    """Create the Streamable HTTP client without resolving secrets at compile time."""

    return MCPClient.streamable_http(
        "${HARNEST_MCP_URL}",
        headers={"Authorization": "Bearer ${HARNEST_MCP_TOKEN}"},
        prefix="fused",
    )
