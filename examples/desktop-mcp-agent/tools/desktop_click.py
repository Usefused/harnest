"""Click a point on the Linux desktop."""

from harnest import context
from harnest.agent import tool


@tool
async def desktop_click(x: int, y: int) -> str:
    """Click a point on the current Linux desktop."""
    await context.resource("desktop").request("POST", "/click", json={"x": x, "y": y})
    return "Clicked. Take a screenshot to inspect the result."
