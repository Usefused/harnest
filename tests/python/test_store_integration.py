"""Opt-in live database tests for built-in production stores."""

import os
import unittest
import uuid

from harnest.checkpoint import CheckpointRecord
from harnest.store import PostgresStore, RedisStore


def _record(run_id: str) -> CheckpointRecord:
    return CheckpointRecord(
        run_id=run_id,
        checkpoint_id="checkpoint-1",
        namespace="graph",
        framework="langgraph",
        type_name="json",
        payload=b"{}",
        metadata_type="json",
        metadata=b"{}",
        versions_type="json",
        versions=b"{}",
    )


class _LiveStoreContract:
    store = None

    async def exercise_store(self):
        token = uuid.uuid4().hex
        user_id = f"integration-user-{token}"
        session_id = f"integration-session-{token}"
        run_id = f"integration-run-{token}"
        await self.store.start()
        try:
            await self.store.create(
                session_id=session_id, user_id=user_id, state={"count": 1}
            )
            async with self.store.acquire(
                session_id=session_id, user_id=user_id
            ) as lease:
                await lease.patch_state({"count": 2})
            session = await self.store.get(
                session_id=session_id, user_id=user_id
            )
            self.assertEqual(session.state["count"], 2)
            await self.store.begin_run(
                application_id="integration",
                user_id=user_id,
                session_id=session_id,
                run_id=run_id,
                framework="langgraph",
            )
            stored = await self.store.put(
                _record(run_id), expected_revision=None
            )
            self.assertEqual(stored.payload, b"{}")
            await self.store.transition(
                run_id=run_id,
                expected_status="running",
                status="completed",
            )
            self.assertEqual(
                (await self.store.get_run(run_id=run_id)).status,
                "completed",
            )
        finally:
            await self.store.delete_run(run_id=run_id)
            await self.store.delete(session_id=session_id, user_id=user_id)
            await self.store.close()


@unittest.skipUnless(
    os.getenv("HARNEST_TEST_POSTGRES_URL"),
    "set HARNEST_TEST_POSTGRES_URL for the live Postgres store test",
)
class LivePostgresStoreTests(_LiveStoreContract, unittest.IsolatedAsyncioTestCase):
    async def test_live_session_checkpoint_and_cas(self):
        self.store = PostgresStore(os.environ["HARNEST_TEST_POSTGRES_URL"])
        await self.exercise_store()


@unittest.skipUnless(
    os.getenv("HARNEST_TEST_REDIS_URL"),
    "set HARNEST_TEST_REDIS_URL for the live Redis store test",
)
class LiveRedisStoreTests(_LiveStoreContract, unittest.IsolatedAsyncioTestCase):
    async def test_live_session_checkpoint_and_cas(self):
        self.store = RedisStore(
            os.environ["HARNEST_TEST_REDIS_URL"],
            prefix=f"harnest-integration-{uuid.uuid4().hex}",
        )
        await self.exercise_store()


if __name__ == "__main__":
    unittest.main()
