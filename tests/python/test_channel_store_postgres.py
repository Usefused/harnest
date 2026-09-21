"""Real PostgreSQL conformance and encoding regression tests for channel storage."""

from __future__ import annotations

from dataclasses import replace
import os
import unittest

from harnest.channel_storage import ChannelStoreConflictError
from harnest.channel_store_postgres import PostgresChannelStore
from harnest.testing_channels import ChannelStoreConformanceMixin


_DSN = os.environ.get("HARNEST_TEST_POSTGRES_DSN")


@unittest.skipUnless(_DSN, "set HARNEST_TEST_POSTGRES_DSN for PostgreSQL conformance")
class PostgresChannelStoreConformanceTests(ChannelStoreConformanceMixin, unittest.IsolatedAsyncioTestCase):
    """Apply the same provider behavior suite to real PostgreSQL transactions."""

    async def make_store(self):
        """Construct an unstarted provider for the shared lifecycle harness."""

        return PostgresChannelStore(_DSN)

    async def asyncSetUp(self):
        """Register scoped record removal before the pool's cleanup runs."""

        await super().asyncSetUp()
        self.addAsyncCleanup(self._clear_scope)

    async def _clear_scope(self):
        """Delete rows created by this case, including its derived cross-scope check."""

        async with self.store._connection() as connection:
            await connection.execute(
                "DELETE FROM harnest_channel_events WHERE installation_id LIKE $1 || '%'", self.installation_id,
            )
            await connection.execute(
                "DELETE FROM harnest_channel_sessions WHERE installation_id LIKE $1 || '%'", self.installation_id,
            )
            await connection.execute(
                "DELETE FROM harnest_channel_replies WHERE installation_id LIKE $1 || '%'", self.installation_id,
            )


@unittest.skipUnless(_DSN, "set HARNEST_TEST_POSTGRES_DSN for PostgreSQL integration")
class PostgresChannelStoreEncodingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = PostgresChannelStore(_DSN)
        await self.store.start()
        self.addAsyncCleanup(self.store.close)
        self.addAsyncCleanup(self._clear_scope)
        self.installation_id = "encoding_" + os.urandom(4).hex()

    async def _clear_scope(self):
        async with self.store._connection() as connection:
            await connection.execute(
                "DELETE FROM harnest_channel_events WHERE installation_id=$1", self.installation_id,
            )
            await connection.execute(
                "DELETE FROM harnest_channel_replies WHERE installation_id=$1", self.installation_id,
            )

    async def test_reply_to_and_receipt_round_trip_through_jsonb(self):
        from harnest.channel_storage import ChannelEvent, ChannelReply

        event = ChannelEvent(
            platform="slack", installation_id=self.installation_id, provider_event_id="e1",
            kind="message", sender_id="U1", conversation_id="C1", message_id="m1",
            occurred_at=1.0, delivery_id="d1", content="hi",
            reply_to={"channel": "C1", "ts": "1.0", "nested": {"a": [1, 2, 3]}},
        )
        stored, is_new = await self.store.admit_event(event)
        self.assertTrue(is_new)
        self.assertEqual(stored.reply_to, {"channel": "C1", "ts": "1.0", "nested": {"a": [1, 2, 3]}})
        fetched = await self.store.get_event(
            platform="slack", installation_id=self.installation_id, provider_event_id="e1",
        )
        self.assertEqual(fetched, stored)

        reply = ChannelReply(
            reply_id="r_" + self.installation_id, platform="slack",
            installation_id=self.installation_id, conversation_id="C1",
            reply_to={"channel": "C1"}, content="hello", created_at=1.0, updated_at=1.0,
        )
        await self.store.enqueue_reply(reply)
        self.assertTrue(await self.store.finish_reply(
            reply_id=reply.reply_id, status="sent", receipt={"ts": "1.1", "ok": True},
        ))
        claimed = await self.store.claim_replies(
            platform="slack", installation_id=self.installation_id, limit=10,
        )
        self.assertEqual(claimed, ())  # sent replies are no longer pending

    async def test_admit_conflict_is_reported_across_a_fresh_connection(self):
        from harnest.channel_storage import ChannelEvent

        event = ChannelEvent(
            platform="slack", installation_id=self.installation_id, provider_event_id="e2",
            kind="message", sender_id="U1", conversation_id="C1", message_id="m1",
            occurred_at=1.0, content="hi",
        )
        await self.store.admit_event(event)
        with self.assertRaises(ChannelStoreConflictError):
            await self.store.admit_event(replace(event, content="different"))

    async def test_restricted_instance_reads_schema_created_by_another(self):
        from harnest.channel_storage import ChannelEvent

        event = ChannelEvent(
            platform="slack", installation_id=self.installation_id, provider_event_id="e3",
            kind="message", sender_id="U1", conversation_id="C1", message_id="m1",
            occurred_at=1.0, content="hi",
        )
        await self.store.admit_event(event)
        second = PostgresChannelStore(_DSN, setup_schema=False)
        await second.start()
        try:
            fetched = await second.get_event(
                platform="slack", installation_id=self.installation_id, provider_event_id="e3",
            )
            self.assertEqual(fetched.sender_id, "U1")
        finally:
            await second.close()
