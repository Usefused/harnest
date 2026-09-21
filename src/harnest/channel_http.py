"""Invoke a served agent through its authenticated, governed response endpoint."""

from __future__ import annotations

from typing import Any

from .channel_storage import ChannelEvent


class ChannelHTTPInvoker:
    """Use caller-owned HTTP transport and explicit per-actor authentication headers.

    The server's authenticator must validate the service credential and map the
    supplied actor identity to its principal. Metadata never establishes identity.
    """

    def __init__(self, client: Any, headers_for_actor: Any) -> None:
        """Keep connection/authentication ownership with the application lifecycle."""
        self.client, self.headers_for_actor = client, headers_for_actor

    async def __call__(self, event: ChannelEvent, session_id: str, actor_id: str) -> str:
        """Create/reuse the scoped session and refuse approval or incomplete responses."""
        headers = self.headers_for_actor(actor_id)
        response = await self.client.post("/sessions", json={"id": session_id}, headers=headers)
        if response.status_code != 409:
            response.raise_for_status()
        response = await self.client.post(
            "/responses", headers=headers,
            json={"input": event.content, "sessionId": session_id, "stream": False},
        )
        response.raise_for_status()
        result = response.json()
        if result.get("status") != "completed" or result.get("requiredAction"):
            raise RuntimeError("channel response requires operator attention")
        text = result.get("outputText")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("channel response has no completed text")
        return text
