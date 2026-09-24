"""Press a key chord on the Linux desktop."""

from harnest import context
from harnest.agent import tool


@tool
async def desktop_key(key: str) -> str:
    """Press one X11 key chord, such as Return or ctrl+l."""
    await context.resource("desktop").request("POST", "/key", json={"key": key})
    return "Key pressed. Take a screenshot to inspect the result."
