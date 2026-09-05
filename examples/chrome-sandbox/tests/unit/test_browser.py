"""Offline checks for the example's browser capability boundary."""

import asyncio


def test_chrome_agent_exposes_the_browser_tool(agent, tools):
    """Keep the compiled example identity and business tool discoverable."""
    assert agent.name == "chrome_researcher"
    assert set(tools) == {"browse_page"}


def test_browser_tool_rejects_an_unapproved_host_without_docker(tools):
    """Validate the safe path used by ordinary offline project tests."""
    result = asyncio.run(tools["browse_page"]("http://127.0.0.1/private"))
    assert result == {"error": "That URL is not on this agent's approved host list."}
