"""Deliver a Slack reply through an explicitly selected Fused SDK operation."""

from __future__ import annotations

from typing import Any, Mapping
from uuid import UUID

from harnest.channel_storage import ChannelReply
from harnest.channels import ChannelError


class FusedSlackReplySender:
    """Call the SDK-scoped Engine API, keeping provider credentials inside Fused.

    The operation must be the exact Slack chat.postMessage selection from the
    immutable app's exported execution schema. The caller owns the HTTP client
    authenticated with that SDK's execution token and an explicit user selector.
    """

    def __init__(self, client: Any, *, app_id: str, operation: str,
                 selector: Mapping[str, str]) -> None:
        """Require a pinned app and connection selector, never infer a bucket or grant."""
        self.app_id = str(UUID(app_id))
        if not operation or not selector.get("end_user_ref"):
            raise ValueError("reply operation and connected end_user_ref are required")
        self.client, self.operation, self.selector = client, operation, dict(selector)

    async def __call__(self, reply: ChannelReply) -> dict[str, str]:
        """Send once, suppress broadcasts/unfurls, and check provider-level success."""
        thread = reply.reply_to.get("thread_id")
        if reply.platform != "slack" or not isinstance(thread, str) or not thread:
            raise ChannelError("Slack replies require an explicit parent thread")
        response = await self.client.post(f"/v1/apps/{self.app_id}/executions", json={
            "operation": self.operation, "selector": self.selector,
            "input": {"channel": reply.conversation_id, "text": reply.content, "thread_ts": thread,
                      "reply_broadcast": False, "unfurl_links": False, "unfurl_media": False},
        })
        response.raise_for_status()
        return _receipt(response.json(), reply)


def _receipt(envelope: Mapping[str, Any], reply: ChannelReply) -> dict[str, str]:
    """HTTP 200 is insufficient: Slack errors are often successful HTTP documents."""
    status = envelope.get("status_code", 0)
    results = envelope.get("results", ())
    if not isinstance(status, int) or not 200 <= status < 300 or len(results) != 1:
        raise ChannelError("Fused reply execution did not succeed")
    result = results[0]
    if result.get("ok") is not True or result.get("channel") != reply.conversation_id or not result.get("ts"):
        raise ChannelError("Slack did not confirm the intended reply")
    return {"message_id": result["ts"]}
