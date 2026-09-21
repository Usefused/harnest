"""Connect generated Fused webhook receivers to durable Harnest channel intake."""

from __future__ import annotations

import math
from typing import Any, Mapping

from harnest.channel_storage import ChannelEvent
from harnest.channels import ChannelError


class FusedChannelReceiver:
    """Own one explicit generated-SDK subscription; ACK only after durable admission."""

    def __init__(self, receiver: Any, worker: Any, normalize: Any) -> None:
        """Accept a caller-created generated receiver without inspecting its credentials."""
        self.receiver, self.worker, self.normalize = receiver, worker, normalize

    def start(self, event_name: str) -> None:
        """Subscribe to the generated enum's exact event value, never a wildcard."""
        if not event_name or event_name in {"ALL", "*"}:
            raise ValueError("an explicit generated webhook event is required")
        self.receiver.on(event_name, self.handle)

    async def handle(self, payload: Mapping[str, Any], context: Mapping[str, Any]) -> None:
        """NACK failed persistence; rejected policy events have no durable side effects."""
        try:
            event = self.normalize(payload)
            if event is not None:
                await self.worker.admit(event)
        except Exception:
            await context["nack"]()
            raise ChannelError("channel event admission failed") from None
        await context["ack"]()

    def close(self) -> None:
        """Stop intake before draining workers and closing their store."""
        self.receiver.close()


def slack_mention(payload: Mapping[str, Any], *, installation_id: str, app_id: str) -> ChannelEvent | None:
    """Normalize a verified Fused Slack envelope, rejecting foreign installations and bots.

    Call only from the authenticated generated SDK receiver. IDs in a raw public
    HTTP request are not proof of provider signature or connection ownership.
    """
    body = payload.get("body", {})
    if body.get("team_id") != installation_id or body.get("api_app_id") != app_id:
        return None
    event = body.get("event", {})
    if event.get("type") != "app_mention" or event.get("bot_id") or event.get("subtype"):
        return None
    required = (body.get("event_id"), event.get("user"), event.get("channel"), event.get("ts"))
    if not all(isinstance(value, str) and value for value in required):
        raise ChannelError("Slack mention is missing a required identity")
    occurred = float(event["ts"])
    if not math.isfinite(occurred):
        raise ChannelError("Slack mention timestamp must be finite")
    return ChannelEvent(
        platform="slack", installation_id=installation_id, provider_event_id=body["event_id"],
        kind="app_mention", sender_id=event["user"], conversation_id=event["channel"],
        message_id=event["ts"], occurred_at=occurred, content=event.get("text", ""),
        thread_id=event.get("thread_ts"),
    )
