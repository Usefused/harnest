"""Reusable conformance checks shipped for custom durable channel storage providers."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any
import uuid

from .channel_storage import ChannelEvent, ChannelReply, ChannelStoreConflictError


class ChannelStoreConformanceMixin:
    """Subclass with IsolatedAsyncioTestCase and an async make_store factory."""

    async def make_store(self) -> Any:
        """Override to return an unstarted provider implementing ChannelStore.

        Tests use a unique installation scope per case. Register backend
        cleanup with addAsyncCleanup; the mixin owns starting and closing it.
        """

        raise NotImplementedError("override make_store to return the provider under test")

    async def asyncSetUp(self) -> None:
        """Give every check its own installation scope and provider lifecycle."""

        self.installation_id = "conformance_" + uuid.uuid4().hex
        self.store = await self.make_store()
        await self.store.start()
        self.addAsyncCleanup(self.store.close)

    def channel_event(self, provider_event_id: str = "evt", **changes: Any) -> ChannelEvent:
        """Build deterministic safe event input independently of provider details."""

        event = ChannelEvent(
            platform="slack", installation_id=self.installation_id,
            provider_event_id=provider_event_id, kind="message", sender_id="alice",
            conversation_id="c1", message_id="m1", occurred_at=1.0,
            delivery_id="d1", content="hello",
        )
        return replace(event, **changes)

    def channel_reply(self, reply_id: str = "reply", **changes: Any) -> ChannelReply:
        """Build one queued reply targeting this scope's default conversation."""

        reply = ChannelReply(
            reply_id=reply_id, platform="slack", installation_id=self.installation_id,
            conversation_id="c1", reply_to={"channel": "c1"}, content="hi",
            created_at=1.0, updated_at=1.0,
        )
        return replace(reply, **changes)

    async def test_admit_is_idempotent_conflict_checked_and_scope_isolated(self) -> None:
        """Concurrent retries admit once and reject a changed redefinition."""

        event = self.channel_event()
        admissions = await asyncio.gather(*(self.store.admit_event(event) for _ in range(8)))
        self.assertEqual(sum(1 for _, is_new in admissions if is_new), 1)
        with self.assertRaises(ChannelStoreConflictError):
            await self.store.admit_event(replace(event, content="different"))
        other = self.channel_event(installation_id=self.installation_id + "-cross")
        _, is_new = await self.store.admit_event(other)
        self.assertTrue(is_new)

    async def test_get_event_reads_only_the_admitted_identity(self) -> None:
        """Reads are scoped to platform, installation, and provider event ID."""

        await self.store.admit_event(self.channel_event())
        found = await self.store.get_event(
            platform="slack", installation_id=self.installation_id, provider_event_id="evt"
        )
        self.assertEqual(found.message_id, "m1")
        missing = await self.store.get_event(
            platform="slack", installation_id=self.installation_id, provider_event_id="absent"
        )
        self.assertIsNone(missing)

    async def test_session_binding_is_first_write_wins(self) -> None:
        """A conversation scope keeps its first bound session across retries."""

        options = dict(
            platform="slack", installation_id=self.installation_id,
            conversation_id="c1", thread_id=None,
        )
        await self.store.bind_session(session_id="session-a", **options)
        await self.store.bind_session(session_id="session-b", **options)
        self.assertEqual(await self.store.get_session_binding(**options), "session-a")
        distinct = await self.store.get_session_binding(
            platform="slack", installation_id=self.installation_id,
            conversation_id="c2", thread_id=None,
        )
        self.assertIsNone(distinct)

    async def test_reply_lifecycle_claims_pending_and_finishes_once(self) -> None:
        """Replies are claimable while pending and terminal exactly once."""

        await self.store.enqueue_reply(self.channel_reply())
        await self.store.enqueue_reply(self.channel_reply(reply_id="reply2", created_at=2.0))
        claimed = await self.store.claim_replies(
            platform="slack", installation_id=self.installation_id, limit=10
        )
        self.assertEqual([item.reply_id for item in claimed], ["reply", "reply2"])
        self.assertTrue(await self.store.finish_reply(reply_id="reply", status="sent"))
        remaining = await self.store.claim_replies(
            platform="slack", installation_id=self.installation_id, limit=10
        )
        self.assertEqual([item.reply_id for item in remaining], ["reply2"])
        self.assertFalse(await self.store.finish_reply(reply_id="missing", status="sent"))
