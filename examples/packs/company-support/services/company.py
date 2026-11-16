from harnest.mcp import MCPClient

def client() -> MCPClient:
    """Connect using credentials from the employee launch environment."""
    return MCPClient.streamable_http("${COMPANY_MCP_URL}", headers={"Authorization": "Bearer ${COMPANY_MCP_TOKEN}"})
