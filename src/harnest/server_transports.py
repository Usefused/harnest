"""Enforce server-wide WebSocket policy across neutral and native routes."""

from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send


class DisabledWebSockets:
    """Reject upgrades before any native or authored WebSocket handler executes."""

    def __init__(self, app: ASGIApp) -> None:
        """Retain the downstream HTTP/lifespan application without taking ownership."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Deny WebSocket handshakes while preserving HTTP and lifespan behavior."""
        # Checking at the ASGI boundary also covers nested native/custom routers.
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": "Live connections are disabled"})
            return
        await self.app(scope, receive, send)


def install_live_policy(app: Any, live_enabled: bool) -> None:
    """Install a server-wide upgrade denial only when live transport is disabled."""
    # Fail on non-booleans instead of treating strings such as "false" as consent.
    if not isinstance(live_enabled, bool):
        raise TypeError("live_enabled must be boolean")
    # Enabled servers keep their normal routing and authentication pipeline.
    if not live_enabled:
        app.add_middleware(DisabledWebSockets)
