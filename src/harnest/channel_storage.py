"""Public durable persistence contract for chat-platform channel messaging."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable


class ChannelStoreConflictError(RuntimeError):
    """An admitted event or reply has a different definition than the request."""


@dataclass(frozen=True, slots=True)
class ChannelEvent:
    """One inbound platform message, normalized by a ChannelAdapter.

    ``delivery_id`` identifies this specific transport delivery and may repeat
    across retries; ``provider_event_id`` is the platform's stable identity for
    deduplication. ``reply_to`` is an opaque, adapter-owned address a later
    reply must target and is never interpreted by the store.
    """

    platform: str
    installation_id: str
    provider_event_id: str
    kind: str
    sender_id: str
    conversation_id: str
    message_id: str
    occurred_at: float
    delivery_id: str = ""
    thread_id: str | None = None
    content: str = field(default="", repr=False)
    reply_to: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class ChannelReply:
    """One outbound answer queued for durable, at-least-once delivery.

    ``status`` is pending, sent, failed, or delivery_unknown (an ambiguous
    send that must be reconciled before replay, never blindly retried).
    """

    reply_id: str
    platform: str
    installation_id: str
    conversation_id: str
    reply_to: Mapping[str, Any] = field(repr=False)
    content: str = field(repr=False)
    status: str = "pending"
    receipt: Mapping[str, Any] | None = field(default=None, repr=False)
    created_at: float = 0.0
    updated_at: float = 0.0


@runtime_checkable
class ChannelStore(Protocol):
    """Atomic operations required for durable, deduplicated channel messaging.

    All identity, scope, and ordering predicates execute inside the datastore.
    A provider must durably commit before returning success.
    """

    async def start(self) -> None:
        """Open owned resources and initialize the provider's durable schema."""
        ...

    async def close(self) -> None:
        """Release owned resources after receivers and senders have stopped."""
        ...

    async def admit_event(self, event: ChannelEvent) -> tuple[ChannelEvent, bool]:
        """Atomically persist a new event or return an identical prior admission.

        Deduplication is scoped by platform, installation, and provider event
        ID. A conflicting definition under the same identity raises
        ChannelStoreConflictError. The second return value is True only for a
        newly admitted event.
        """
        ...

    async def get_event(
        self, *, platform: str, installation_id: str, provider_event_id: str
    ) -> ChannelEvent | None:
        """Read one admitted event by its deduplication identity."""
        ...

    async def bind_session(
        self, *, platform: str, installation_id: str, conversation_id: str,
        thread_id: str | None, session_id: str,
    ) -> None:
        """Idempotently record which Harnest session owns a conversation scope."""
        ...

    async def get_session_binding(
        self, *, platform: str, installation_id: str, conversation_id: str,
        thread_id: str | None,
    ) -> str | None:
        """Read the session bound to a conversation scope, if any."""
        ...

    async def enqueue_reply(self, reply: ChannelReply) -> ChannelReply:
        """Durably queue one reply before the adapter sends it."""
        ...

    async def claim_replies(
        self, *, platform: str, installation_id: str, limit: int = 1
    ) -> tuple[ChannelReply, ...]:
        """Claim a bounded batch of pending replies for one sender to send."""
        ...

    async def finish_reply(
        self, *, reply_id: str, status: str, receipt: Mapping[str, Any] | None = None
    ) -> bool:
        """Record a claimed reply's terminal outcome; unknown IDs return false."""
        ...


__all__ = [
    "ChannelEvent",
    "ChannelReply",
    "ChannelStore",
    "ChannelStoreConflictError",
]
