"""Start one desktop before the agent serves requests and close it at shutdown."""

import asyncio

from harnest import context, lifecycle
from harnest.lib.desktop import Desktop


@lifecycle.resource
@context.provider("desktop")
async def desktop():
    """Own exactly one Docker desktop for this running agent instance."""
    owner = await asyncio.to_thread(Desktop.start)
    try:
        yield owner
    finally:
        await asyncio.to_thread(owner.close)
