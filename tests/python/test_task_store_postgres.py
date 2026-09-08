"""Real PostgreSQL conformance and transaction regression tests."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import os
import unittest
from unittest.mock import patch
import uuid

from harnest.cron_storage import CronRecord, CronStoreConflictError
from harnest.task_storage import TaskRecord, TaskStoreConflictError
from harnest.task_store_postgres import PostgresTaskStore
from harnest.testing import TaskStoreConformanceMixin
from harnest_postgres import PostgresStore


_DSN = os.environ.get("HARNEST_TEST_POSTGRES_DSN")


@unittest.skipUnless(_DSN, "set HARNEST_TEST_POSTGRES_DSN for PostgreSQL conformance")
class PostgresTaskStoreConformanceTests(TaskStoreConformanceMixin, unittest.IsolatedAsyncioTestCase):
    """Apply the same provider behavior suite to real PostgreSQL transactions."""

    async def make_store(self):
        """Construct an unstarted provider for the shared lifecycle harness."""

        return PostgresTaskStore(_DSN)

    async def asyncSetUp(self):
        """Register scoped record removal before the pool's cleanup runs."""

        await super().asyncSetUp()
        self.addAsyncCleanup(self._clear_scope)

    async def _clear_scope(self):
        """Delete only application scopes created by this conformance case."""

        async with self.store._connection() as connection:
            await connection.execute(
                "DELETE FROM harnest_durable_tasks WHERE application_id=$1", self.application_id,
            )
            await connection.execute(
                "DELETE FROM harnest_durable_crons WHERE application_id=$1", self.application_id,
            )


@unittest.skipUnless(_DSN, "set HARNEST_TEST_POSTGRES_DSN for PostgreSQL integration")
class PostgresTaskStoreIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Exercise database guarantees that a mock SQL interpreter cannot prove."""

    async def asyncSetUp(self) -> None:
        """Use a fresh application scope while sharing the additive schema."""

        self.application_id = f"provider-test-{uuid.uuid4().hex}"
        self.store = PostgresTaskStore(_DSN)
        await self.store.start()
        self.addAsyncCleanup(self.store.close)
        self.addAsyncCleanup(self._clear_scope)

    async def _clear_scope(self) -> None:
        """Remove only records belonging to this test's generated application."""

        async with self.store._connection() as connection:
            await connection.execute(
                "DELETE FROM harnest_durable_tasks WHERE application_id=$1", self.application_id,
            )
            await connection.execute(
                "DELETE FROM harnest_durable_crons WHERE application_id=$1", self.application_id,
            )

    def job(self, suffix: str = "1", **options) -> TaskRecord:
        """Build one valid, owner-scoped pending job."""

        record = TaskRecord(
            job_id=f"job-{suffix}", application_id=self.application_id, user_id="alice",
            task_name="deliver", queue="default", arguments={"message": "private"},
            invocation={"user_id": "alice"}, agent_permissions=("deliver",), scheduled_at=10,
        )
        return replace(record, **options)

    def schedule(self, suffix: str = "1", **options) -> CronRecord:
        """Build a UTC schedule whose first occurrence is already due."""

        record = CronRecord(
            schedule_id=f"cron-{suffix}", application_id=self.application_id, user_id="alice",
            key=f"key-{suffix}", expression="* * * * *", task_name="deliver",
            arguments={"message": "private"}, next_run_at=10,
        )
        return replace(record, **options)

    async def test_concurrent_claims_and_expired_attempt_fencing(self) -> None:
        """Independent SQL claims cannot double-reserve an unexpired attempt."""

        await asyncio.gather(*(self.store.enqueue_task(self.job(str(i))) for i in range(8)))
        batches = await asyncio.gather(*(
            self.store.claim_tasks(
                application_id=self.application_id, queues=("default",), now=10,
                lease_seconds=5, limit=2,
            ) for _ in range(4)
        ))
        jobs = [job for batch in batches for job in batch]
        self.assertEqual(len({job.job_id for job in jobs}), 8)
        recovered = await self.store.claim_tasks(
            application_id=self.application_id, queues=("default",), now=15,
            lease_seconds=5, limit=8,
        )
        self.assertEqual({job.attempt for job in recovered}, {2})
        self.assertFalse(await self.store.finish_task(
            application_id=self.application_id, job_id=jobs[0].job_id,
            lease_token=jobs[0].lease_token, now=16, status="completed", result="stale",
        ))

    async def test_terminal_scrub_preserves_exact_idempotency(self) -> None:
        """The immutable fingerprint survives erasure of sensitive task inputs."""

        original = self.job(idempotency_key="stable")
        await self.store.enqueue_task(original)
        claimed, = await self.store.claim_tasks(
            application_id=self.application_id, queues=("default",), now=10, lease_seconds=5,
        )
        await self.store.finish_task(
            application_id=self.application_id, job_id=claimed.job_id,
            lease_token=claimed.lease_token, now=11, status="completed", result={"sent": True},
        )
        replay = await self.store.enqueue_task(replace(original, job_id="new-id", scheduled_at=50))
        self.assertEqual(replay.job_id, original.job_id)
        self.assertEqual(replay.status, "completed")
        self.assertEqual(replay.arguments, {})
        self.assertIsNone(replay.invocation)
        self.assertIsNone(replay.agent_permissions)
        with self.assertRaises(TaskStoreConflictError):
            await self.store.enqueue_task(replace(original, arguments={"different": True}))

    async def test_expiry_sweep_is_bounded_and_skips_another_workers_lock(self) -> None:
        """One exhausted lease cannot block or expand another replica's sweep."""

        for index in range(4):
            await self.store.enqueue_task(self.job(str(index), max_retries=0))
        await self.store.claim_tasks(
            application_id=self.application_id, queues=("default",), now=10,
            lease_seconds=5, limit=4,
        )
        async with self.store._connection() as connection:
            async with connection.transaction():
                await connection.fetchrow(
                    "SELECT job_id FROM harnest_durable_tasks "
                    "WHERE application_id=$1 AND job_id='job-0' FOR UPDATE",
                    self.application_id,
                )
                self.assertEqual(await asyncio.wait_for(self.store.claim_tasks(
                    application_id=self.application_id, queues=("default",), now=15,
                    lease_seconds=5, limit=1,
                ), timeout=2), ())
                rows = await connection.fetch(
                    "SELECT job_id,status FROM harnest_durable_tasks "
                    "WHERE application_id=$1 ORDER BY job_id", self.application_id,
                )
        self.assertEqual(
            [(row["job_id"], row["status"]) for row in rows],
            [("job-0", "running"), ("job-1", "failed"), ("job-2", "running"), ("job-3", "running")],
        )
        await self.store.claim_tasks(
            application_id=self.application_id, queues=("default",), now=15,
            lease_seconds=5, limit=1,
        )
        async with self.store._connection() as connection:
            count = await connection.fetchval(
                "SELECT count(*) FROM harnest_durable_tasks "
                "WHERE application_id=$1 AND status='failed'", self.application_id,
            )
        self.assertEqual(count, 2)

    async def test_occurrence_is_atomic_and_rolls_back_on_enqueue_conflict(self) -> None:
        """A failed handoff neither advances the schedule nor loses due work."""

        schedule = await self.store.create_cron(self.schedule())
        task = self.job(idempotency_key="occurrence")
        await self.store.enqueue_task(replace(task, arguments={"unrelated": True}))
        with self.assertRaises(TaskStoreConflictError):
            await self.store.commit_cron_occurrence(
                application_id=self.application_id, user_id="alice", schedule_id=schedule.schedule_id,
                expected_revision=0, due_at=10, next_run_at=70, task=task,
            )
        current = await self.store.get_cron(
            application_id=self.application_id, user_id="alice", schedule_id=schedule.schedule_id,
        )
        self.assertEqual(current.next_run_at, 10)
        self.assertEqual(current.revision, 0)

    async def test_simultaneous_dispatch_commits_only_one_occurrence(self) -> None:
        """A row revision serializes separate dispatchers in one transaction."""

        schedule = await self.store.create_cron(self.schedule())

        async def commit(number):
            """Race independent occurrence candidates against the same cursor."""

            return await self.store.commit_cron_occurrence(
                application_id=self.application_id, user_id="alice", schedule_id=schedule.schedule_id,
                expected_revision=0, due_at=10, next_run_at=70,
                task=self.job(str(number), idempotency_key="same-occurrence"),
            )

        results = await asyncio.gather(*(commit(i) for i in range(6)))
        self.assertEqual(sum(item is not None for item in results), 1)
        async with self.store._connection() as connection:
            count = await connection.fetchval(
                "SELECT count(*) FROM harnest_durable_tasks WHERE application_id=$1", self.application_id,
            )
        self.assertEqual(count, 1)

    async def test_exhausted_crash_and_retry_scrub_terminal_payload(self) -> None:
        """Both crash recovery and explicit retry obey the attempt budget."""

        await self.store.enqueue_task(self.job(max_retries=0))
        await self.store.claim_tasks(
            application_id=self.application_id, queues=("default",), now=10, lease_seconds=5,
        )
        self.assertEqual(await self.store.claim_tasks(
            application_id=self.application_id, queues=("default",), now=15, lease_seconds=5,
        ), ())
        failed = await self.store.get_task(application_id=self.application_id, job_id="job-1")
        self.assertEqual((failed.status, failed.failure_code, failed.arguments), ("failed", "task_failed", {}))
        await self.store.enqueue_task(self.job("2", max_retries=0))
        claimed, = await self.store.claim_tasks(
            application_id=self.application_id, queues=("default",), now=20, lease_seconds=5,
        )
        self.assertTrue(await self.store.finish_task(
            application_id=self.application_id, job_id=claimed.job_id,
            lease_token=claimed.lease_token, now=21, status="pending", retry_at=30,
        ))
        failed = await self.store.get_task(application_id=self.application_id, job_id="job-2")
        self.assertEqual((failed.status, failed.failure_code, failed.arguments), ("failed", "task_failed", {}))

    async def test_due_and_owner_pagination_use_one_query(self) -> None:
        """Database predicates page results without hidden per-row lookups."""

        await asyncio.gather(*(
            self.store.create_cron(self.schedule(str(i), next_run_at=10+i)) for i in range(4)
        ))
        await self.store.create_cron(self.schedule("foreign", user_id="bob"))
        calls = []
        original = self.store._connection

        class CountedConnection:
            async def fetch(self, query, *values):
                """Count actual database reads without interpreting their SQL."""

                calls.append(query)
                return await connection.fetch(query, *values)

        @asynccontextmanager
        async def counted():
            """Wrap the existing pool for query-count assertions only."""

            nonlocal connection
            async with original() as connection:
                yield CountedConnection()

        connection = None
        with patch.object(self.store, "_connection", counted):
            page = await self.store.list_crons(
                application_id=self.application_id, user_id="alice", after="cron-0", limit=2,
            )
            self.assertEqual([record.schedule_id for record in page], ["cron-1", "cron-2"])
            self.assertEqual(len(calls), 1)
            due = await self.store.list_due_crons(
                application_id=self.application_id, now=12, after=(10, "cron-foreign"), limit=1,
            )
            self.assertEqual([record.schedule_id for record in due], ["cron-1"])
            self.assertEqual(len(calls), 2)

    async def test_cancelled_cron_is_terminal_and_owner_scoped(self) -> None:
        """CAS prevents reactivation while exact repeated cancellation is stable."""

        original = await self.store.create_cron(self.schedule())
        cancelled = await self.store.update_cron(replace(original, status="cancelled"), expected_revision=0)
        self.assertEqual(await self.store.update_cron(cancelled, expected_revision=1), cancelled)
        with self.assertRaises(CronStoreConflictError):
            await self.store.update_cron(replace(cancelled, status="active"), expected_revision=1)
        self.assertIsNone(await self.store.get_cron(
            application_id=self.application_id, user_id="bob", schedule_id=original.schedule_id,
        ))
        with self.assertRaises(KeyError):
            await self.store.update_cron(replace(original, user_id="bob"), expected_revision=0)

    async def test_fresh_provider_reads_committed_jobs(self) -> None:
        """A new pool observes durable data independently of process-local state."""

        original = await self.store.enqueue_task(self.job())
        second = PostgresTaskStore(_DSN, setup_schema=False)
        await second.start()
        try:
            self.assertEqual(await second.get_task(
                application_id=self.application_id, user_id="alice", job_id=original.job_id,
            ), original)
        finally:
            await second.close()

    async def test_package_combines_storage_roles_on_one_pool(self) -> None:
        """The bundled provider reuses one pool for sessions and durable work."""

        combined = PostgresStore(_DSN, _pool=self.store._pool)
        await combined.start()
        try:
            self.assertIs(combined._pool, self.store._pool)
            self.assertIs(combined._lease_pool, self.store._pool)
            await combined.enqueue_task(self.job())
            self.assertIsNotNone(await self.store.get_task(
                application_id=self.application_id, job_id="job-1", user_id="alice",
            ))
        finally:
            await combined.close()
        # Closing a combined wrapper must not close its caller-owned pool.
        self.assertIsNotNone(await self.store.get_task(
            application_id=self.application_id, job_id="job-1", user_id="alice",
        ))
