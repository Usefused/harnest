"""Explicit, invocation-scoped events for application-owned user interfaces."""

from typing import Any


async def emit(name: str, value: Any) -> None:
    """Send a named JSON value to the caller without adding it to model history.

    AG-UI exposes this as CUSTOM; native transports expose ui_event. Use public
    display data only. Values are limited to 64 KiB, with 128 events and 512 KiB
    per invocation. Streamed emissions apply backpressure and are not durable.
    """
    from .context import current
    active = current()
    active._require_active()
    await active._ui_output.emit(name, value, agent=active.agent_name)


__all__ = ["emit"]
