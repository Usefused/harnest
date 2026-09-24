"""Navigate the Chrome tab kept alive inside the Linux desktop."""

from harnest import context
from harnest.agent import tool


@tool
async def browser_navigate(url: str) -> dict[str, object]:
    """Navigate the persistent visible Chrome tab to an HTTP(S) URL."""
    response = await context.resource("desktop").request("POST", "/navigate", json={"url": url})
    return response.json()
