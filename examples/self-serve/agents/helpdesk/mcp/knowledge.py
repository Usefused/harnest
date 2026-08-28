from harnest.mcp import MCPClient


def client() -> MCPClient:
    """Create the filename-identified knowledge connection."""

    return MCPClient.streamable_http(
        "http://127.0.0.1:9000/mcp",
        tools=("search_articles", "get_article"),
        prefix="knowledge",
    )
