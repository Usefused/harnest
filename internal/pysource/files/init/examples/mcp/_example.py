"""Copy to knowledge.py and configure HARNEST_MCP_URL to enable this client.

Keep credentials in the runtime environment, never in this source file.
"""

import os

from harnest.mcp import MCPClient


def client():
    """Resolve the authored endpoint when Harnest discovers this client."""
    return MCPClient.streamable_http(os.environ["HARNEST_MCP_URL"], prefix="knowledge")
