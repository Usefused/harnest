"""In-process channel store reference provider for tests and local development."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, Mapping

from .channel_storage import ChannelEvent, ChannelReply, ChannelStoreConflictError


class MemoryChannelStore:
    """Implement the atomic channel contract in one process; nothing survives restart."""

    def __init__(self) -> None:
        """Create isolated indexes and one transaction lock for both capabilities."""

        self._lock = asyncio.Lock()
        self._events: dict[tuple[str, str, str], ChannelEvent] = {}
        self._sessions: dict[tuple[str, str, str, str], str] = {}
        self._replies: dict[str, ChannelReply] = {}

    async def start(self) -> None:
        """Satisfy lifecycle ownership without opening external resources."""

    async def close(self) -> None:
        """Release no resources; in-memory data lives only with this instance."""

    async def admit_event(self, event: ChannelEvent) -> tuple[ChannelEvent, bool]:
        """Commit an isolated event snapshot keyed by its deduplication identity."""

        async with self._lock:
            key = _event_key(event)
            existing = self._events.get(key)
            if existing is not None:
                if existing != event:
                    raise ChannelStoreConflictError(
                        "channel event identity has a different definition"
                    )
                return existing, False
            self._events[key] = event
            return event, True

    async def get_event(
        self, *, platform: str, installation_id: str, provider_event_id: str
    ) -> ChannelEvent | None:
        """Read one detached event snapshot by its deduplication identity."""

        async with self._lock:
            return self._events.get((platform, installation_id, provider_event_id))

    async def bind_session(
        self, *, platform: str, installation_id: str, conversation_id: str,
        thread_id: str | None, session_id: str,
    ) -> None:
        """Record the session owning a conversation scope without overwriting it."""

        async with self._lock:
            key = _session_key(platform, installation_id, conversation_id, thread_id)
            self._sessions.setdefault(key, session_id)

    async def get_session_binding(
        self, *, platform: str, installation_id: str, conversation_id: str,
        thread_id: str | None,
    ) -> str | None:
        """Read the session bound to a conversation scope, if any."""

        async with self._lock:
            return self._sessions.get(
                _session_key(platform, installation_id, conversation_id, thread_id)
            )

    async def enqueue_reply(self, reply: ChannelReply) -> ChannelReply:
        """Queue one detached reply snapshot for later claim and send."""

        async with self._lock:
            self._replies[reply.reply_id] = reply
            return reply

    async def claim_replies(
        self, *, platform: str, installation_id: str, limit: int = 1
    ) -> tuple[ChannelReply, ...]:
        """Return a bounded scoped batch of pending replies, oldest first."""

        _validate_limit(limit)
        async with self._lock:
            pending = sorted(
                (item for item in self._replies.values()
                 if item.platform == platform and item.installation_id == installation_id
                 and item.status == "pending"),
                key=lambda item: (item.created_at, item.reply_id),
            )
            return tuple(pending[:limit])

    async def finish_reply(
        self, *, reply_id: str, status: str, receipt: Mapping[str, Any] | None = None
    ) -> bool:
        """Record a terminal outcome for one previously enqueued reply."""

        _validate_status(status)
        async with self._lock:
            reply = self._replies.get(reply_id)
            if reply is None:
                return False
            self._replies[reply_id] = replace(reply, status=status, receipt=receipt)
            return True


def _event_key(event: ChannelEvent) -> tuple[str, str, str]:
    """Scope deduplication to platform, installation, and provider event ID."""

    return event.platform, event.installation_id, event.provider_event_id


def _session_key(
    platform: str, installation_id: str, conversation_id: str, thread_id: str | None
) -> tuple[str, str, str, str]:
    """Fold an absent thread into the conversation-level session scope."""

    return platform, installation_id, conversation_id, thread_id or ""


def _validate_limit(limit: int) -> None:
    """Reject unbounded or ambiguous claim batch sizes."""

    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000")


def _validate_status(status: str) -> None:
    """Reject outcomes outside the durable reply status contract."""

    if status not in {"sent", "failed", "delivery_unknown"}:
        raise ValueError("finish status must be sent, failed, or delivery_unknown")


__all__ = ["MemoryChannelStore"]
