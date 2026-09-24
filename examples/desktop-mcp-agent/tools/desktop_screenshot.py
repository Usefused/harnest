"""Return a view of the complete Linux desktop."""

import base64

from harnest import context
from harnest.agent import tool
from harnest.content import Image


@tool
async def desktop_screenshot() -> Image:
    """Capture the whole 1280×800 Linux desktop as an image."""
    response = await context.resource("desktop").request("GET", "/screenshot")
    return Image(data=base64.b64encode(response.content).decode("ascii"), mediaType="image/png")
