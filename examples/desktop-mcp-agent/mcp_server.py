"""Expose one running Harnest desktop agent as a Streamable HTTP MCP tool."""

from __future__ import annotations

import argparse

import httpx
from fastmcp import FastMCP


mcp = FastMCP("Harnest desktop agent")
_agent_url = "http://127.0.0.1:1907"


@mcp.tool
async def ask_agent(prompt: str, session_id: str | None = None) -> dict[str, str]:
    """Send a turn to the agent and return its answer and reusable session ID."""
    payload = {"input": prompt}
    if session_id:
        payload["sessionId"] = session_id
    async with httpx.AsyncClient(base_url=_agent_url, timeout=120) as client:
        response = await client.post("/responses", json=payload)
        response.raise_for_status()
    result = response.json()
    return {
        "status": result["status"],
        "text": result.get("outputText", ""),
        "session_id": result["sessionId"],
    }


def main() -> None:
    """Bind one MCP endpoint to the Harnest process started by provision.py."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-url", required=True)
    parser.add_argument("--port", type=int, required=True)
    arguments = parser.parse_args()
    global _agent_url
    _agent_url = arguments.agent_url
    mcp.run(transport="http", host="127.0.0.1", port=arguments.port, show_banner=False)


if __name__ == "__main__":
    main()
