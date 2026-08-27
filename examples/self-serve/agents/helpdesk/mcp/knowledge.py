import os

from harnest.mcp import MCPClient


_url = os.getenv("KNOWLEDGE_MCP_URL")
_token = os.getenv("KNOWLEDGE_MCP_TOKEN")

# Compilation accepts None. Requiring the complete pair keeps local compilation
# and deployments with a partially configured integration safe and deterministic.
knowledge: MCPClient | None = (
    MCPClient.streamable_http(
        "${KNOWLEDGE_MCP_URL}",
        headers={"Authorization": "Bearer ${KNOWLEDGE_MCP_TOKEN}"},
        tools=("search_articles", "get_article"),
        prefix="knowledge",
    )
    if _url and _token
    else None
)
