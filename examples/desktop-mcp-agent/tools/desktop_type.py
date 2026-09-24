"""Type into the focused Linux desktop window."""

from harnest import context
from harnest.agent import tool


@tool
async def desktop_type(text: str) -> str:
    """Type text into the focused desktop control."""
    await context.resource("desktop").request("POST", "/type", json={"text": text})
    return "Typed. Take a screenshot to inspect the result."
