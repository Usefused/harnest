"""Reusable conformance checks shipped for custom durable storage providers."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import uuid
from typing import Any

from .cron_storage import CronRecord, CronStoreConflictError
from .task_storage import TaskRecord, TaskStoreConflictError


class TaskStoreConformanceMixin:
    """Subclass with IsolatedAsyncioTestCase and an async make_store factory."""

    async def make_store(self) -> Any:
        """Override to return an unstarted provider implementing both contracts.

        Tests use a unique application scope per case. Register backend cleanup
        with addAsyncCleanup; the mixin owns starting and closing the provider.
        """

        raise NotImplementedError("override make_store to return the provider under test")

    async def asyncSetUp(self) -> None:
        """Give every check its own application scope and provider lifecycle."""

        self.application_id = "conformance_" + uuid.uuid4().hex
        self.store = await self.make_store()
        await self.store.start()
        self.addAsyncCleanup(self.store.close)

    def task_record(self, job_id: str = "job", **changes: Any) -> TaskRecord:
        """Build deterministic safe job input independently of provider details."""

        record = TaskRecord(
            job_id=job_id, application_id=self.application_id, user_id="alice",
            task_name="deliver", queue="default", arguments={"items": [1]},
            invocation={"user_id": "alice"}, agent_permissions=("read",),
            scheduled_at=10.0, created_at=1.0, updated_at=1.0,
        )
        return replace(record, **changes)

    def cron_record(self, schedule_id: str = "cron", **changes: Any) -> CronRecord:
        """Build one due user schedule with stable ownership and definition."""

        record = CronRecord(
            schedule_id=schedule_id, application_id=self.application_id,
            user_id="alice", key=schedule_id, expression="* * * * *",
            task_name="deliver", arguments={"items": [1]}, next_run_at=60.0,
            created_at=1.0, updated_at=1.0,
        )
        return replace(record, **changes)

    async def claim(self, **changes: Any) -> tuple[TaskRecord, ...]:
        """Claim one bounded batch through the shared worker contract."""

        options = dict(application_id=self.application_id, queues=("default",),
                       now=10.0, lease_seconds=5.0, limit=1)
        options.update(changes)
        return await self.store.claim_tasks(**options)

    async def read_task(self, job_id: str = "job", user_id: str = "alice") -> TaskRecord | None:
        """Read a task as the selected owner."""

        return await self.store.get_task(application_id=self.application_id,
                                         user_id=user_id, job_id=job_id)

    async def finish(self, record: TaskRecord, **changes: Any) -> bool:
        """Finish the exact lease with deterministic test timestamps."""

        options = dict(application_id=self.application_id, job_id=record.job_id,
                       lease_token=record.lease_token, now=11.0, status="completed",
                       result={"ok": True})
        options.update(changes)
        return await self.store.finish_task(**options)

    async def test_enqueue_is_atomic_idempotent_and_owner_scoped(self) -> None:
        """Concurrent retries produce one job without crossing task/user scope."""

        record = self.task_record(idempotency_key="same")
        jobs = await asyncio.gather(*(self.store.enqueue_task(replace(record, job_id=f"retry{i}")) for i in range(8)))
        self.assertEqual(len({item.job_id for item in jobs}), 1)
        other = await self.store.enqueue_task(replace(record, job_id="other", user_id="bob"))
        self.assertEqual(other.user_id, "bob")
        with self.assertRaises(TaskStoreConflictError):
            await self.store.enqueue_task(replace(record, arguments={"items": [2]}))
        self.assertIsNone(await self.read_task(jobs[0].job_id, "bob"))

    async def test_completed_task_retains_idempotency_but_scrubs_payload(self) -> None:
        """Private inputs disappear while retained fingerprints deduplicate retries."""

        record = self.task_record(idempotency_key="retained")
        await self.store.enqueue_task(record)
        claimed, = await self.claim()
        self.assertTrue(await self.finish(claimed))
        terminal = await self.read_task()
        self.assertEqual(terminal.arguments, {})
        self.assertIsNone(terminal.invocation)
        self.assertIsNone(terminal.agent_permissions)
        self.assertEqual(terminal.result, {"ok": True})
        replay = await self.store.enqueue_task(replace(record, job_id="replay", scheduled_at=1000.0))
        self.assertEqual((replay.job_id, replay.status), ("job", "completed"))

    async def test_claim_filters_due_queue_and_application_and_never_duplicates(self) -> None:
        """Replicas atomically claim distinct jobs using datastore scope predicates."""

        for record in (self.task_record("a"), self.task_record("b"),
                       self.task_record("future", scheduled_at=100.0),
                       self.task_record("queue", queue="other"),
                       self.task_record("foreign", application_id="foreign")):
            await self.store.enqueue_task(record)
        batches = await asyncio.gather(self.claim(), self.claim(), self.claim())
        self.assertEqual(sorted(item.job_id for batch in batches for item in batch), ["a", "b"])
        self.assertEqual(await self.claim(queues=()), ())

    async def test_expired_lease_cannot_finish_and_reclaim_uses_new_token(self) -> None:
        """Fencing rejects old workers before and after a replacement claim."""

        await self.store.enqueue_task(self.task_record())
        first, = await self.claim()
        self.assertFalse(await self.finish(first, now=15.0))
        self.assertFalse(await self.store.renew_task_lease(
            application_id=self.application_id, job_id="job", lease_token=first.lease_token,
            now=15.0, lease_seconds=10.0))
        second, = await self.claim(now=15.0)
        self.assertEqual(second.attempt, 2)
        self.assertNotEqual(second.lease_token, first.lease_token)
        self.assertFalse(await self.finish(first, now=16.0))
        self.assertTrue(await self.finish(second, now=16.0))

    async def test_renewal_prevents_early_reclaim(self) -> None:
        """A valid heartbeat extends exclusive attempt ownership."""

        await self.store.enqueue_task(self.task_record())
        claimed, = await self.claim()
        self.assertTrue(await self.store.renew_task_lease(
            application_id=self.application_id, job_id="job", lease_token=claimed.lease_token,
            now=14.0, lease_seconds=10.0))
        self.assertEqual(await self.claim(now=16.0), ())
        self.assertTrue(await self.finish(claimed, now=20.0))

    async def test_retry_delay_and_exhausted_crash_recovery(self) -> None:
        """Retries retain inputs and crashed final attempts reach a terminal state."""

        await self.store.enqueue_task(self.task_record(max_retries=1))
        first, = await self.claim()
        self.assertTrue(await self.finish(first, status="pending", retry_at=20.0, result=None))
        self.assertEqual(await self.claim(now=19.0), ())
        second, = await self.claim(now=20.0)
        self.assertEqual(second.arguments, {"items": [1]})
        self.assertEqual(await self.claim(now=25.0), ())
        terminal = await self.read_task()
        self.assertEqual((terminal.status, terminal.failure_code), ("failed", "task_failed"))

    async def test_cancellation_is_scoped_terminal_and_fences_running_attempt(self) -> None:
        """Cancellation cannot reveal another owner's work or accept late results."""

        await self.store.enqueue_task(self.task_record())
        claimed, = await self.claim()
        options = dict(application_id=self.application_id, job_id="job", now=11.0)
        self.assertFalse(await self.store.cancel_task(user_id="bob", **options))
        self.assertTrue(await self.store.cancel_task(user_id="alice", **options))
        self.assertFalse(await self.finish(claimed, now=12.0))
        self.assertFalse(await self.store.cancel_task(user_id="alice", **options))
        self.assertEqual((await self.read_task()).status, "cancelled")

    async def test_snapshots_do_not_share_mutable_payload(self) -> None:
        """Mutating submitted or returned objects cannot rewrite durable records."""

        record = self.task_record()
        returned = await self.store.enqueue_task(record)
        record.arguments["items"].append(2)
        returned.arguments["items"].append(3)
        self.assertEqual((await self.read_task()).arguments, {"items": [1]})

    async def test_cron_idempotency_revision_and_terminal_cancellation(self) -> None:
        """Definition retry preserves inactive state and stale edits cannot win."""

        original = self.cron_record()
        stored = await self.store.create_cron(original)
        cancelled = await self.store.update_cron(replace(stored, status="cancelled"), expected_revision=0)
        replay = await self.store.create_cron(replace(original, schedule_id="replay"))
        self.assertEqual((replay.schedule_id, replay.status), ("cron", "cancelled"))
        with self.assertRaises(CronStoreConflictError):
            await self.store.update_cron(replace(cancelled, status="active"), expected_revision=cancelled.revision)
        with self.assertRaises(CronStoreConflictError):
            await self.store.create_cron(replace(original, expression="0 * * * *"))

    async def test_cron_lists_enforce_owner_due_state_and_keyset_pagination(self) -> None:
        """Due pagination includes equal timestamps without leaking foreign scopes."""

        for record in (self.cron_record("a"), self.cron_record("b"),
                       self.cron_record("paused", status="paused"),
                       self.cron_record("future", next_run_at=120.0),
                       self.cron_record("bob", user_id="bob"),
                       self.cron_record("foreign", application_id="foreign")):
            await self.store.create_cron(record)
        page = await self.store.list_crons(application_id=self.application_id, user_id="alice", limit=1)
        self.assertEqual([item.schedule_id for item in page], ["a"])
        next_page = await self.store.list_crons(application_id=self.application_id, user_id="alice", after="a", limit=1)
        self.assertEqual([item.schedule_id for item in next_page], ["b"])
        due = await self.store.list_due_crons(application_id=self.application_id, now=60.0, after=(60.0, "a"), limit=10)
        self.assertEqual([item.schedule_id for item in due], ["b", "bob"])

    async def test_cron_occurrence_commit_is_atomic_and_exactly_deduplicated(self) -> None:
        """Concurrent dispatchers advance once and commit exactly one runnable job."""

        await self.store.create_cron(self.cron_record())
        options = dict(application_id=self.application_id, user_id="alice", schedule_id="cron",
                       expected_revision=0, due_at=60.0, next_run_at=120.0,
                       task=self.task_record("occurrence", idempotency_key="cron:60"))
        results = await asyncio.gather(*(self.store.commit_cron_occurrence(**options) for _ in range(8)))
        self.assertEqual(sum(item is not None for item in results), 1)
        current = await self.store.get_cron(application_id=self.application_id, user_id="alice", schedule_id="cron")
        self.assertEqual((current.revision, current.next_run_at), (1, 120.0))
        self.assertIsNotNone(await self.read_task("occurrence"))

    async def test_cancelled_cron_cannot_commit_and_conflict_rolls_back_cursor(self) -> None:
        """A cancelled/stale schedule or failed enqueue leaves no partial handoff."""

        record = await self.store.create_cron(self.cron_record())
        await self.store.enqueue_task(self.task_record("collision", arguments={"different": True}))
        options = dict(application_id=self.application_id, user_id="alice", schedule_id="cron",
                       expected_revision=0, due_at=60.0, next_run_at=120.0,
                       task=self.task_record("collision"))
        with self.assertRaises(TaskStoreConflictError):
            await self.store.commit_cron_occurrence(**options)
        unchanged = await self.store.get_cron(application_id=self.application_id, user_id="alice", schedule_id="cron")
        self.assertEqual(unchanged.next_run_at, 60.0)
        cancelled = await self.store.update_cron(replace(record, status="cancelled"), expected_revision=0)
        options.update(expected_revision=cancelled.revision, task=self.task_record("new"))
        self.assertIsNone(await self.store.commit_cron_occurrence(**options))
        self.assertIsNone(await self.read_task("new"))

    async def test_occurrence_cannot_substitute_schedule_arguments(self) -> None:
        """Atomic handoff must preserve the validated schedule's authored input."""

        await self.store.create_cron(self.cron_record())
        with self.assertRaises(CronStoreConflictError):
            await self.store.commit_cron_occurrence(
                application_id=self.application_id, user_id="alice", schedule_id="cron",
                expected_revision=0, due_at=60.0, next_run_at=120.0,
                task=self.task_record(arguments={"items": ["substituted"]}),
            )
        self.assertIsNone(await self.read_task())

    async def test_limits_reject_unbounded_or_ambiguous_requests(self) -> None:
        """Providers share the public bounded-page contract before datastore I/O."""

        for limit in (0, -1, True, 1001):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    await self.claim(limit=limit)
                with self.assertRaises(ValueError):
                    await self.store.list_crons(application_id=self.application_id,
                                                user_id="alice", limit=limit)
