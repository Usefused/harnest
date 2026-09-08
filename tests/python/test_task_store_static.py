"""Verify static schedule deployment reconciliation and provider safety seams."""

from dataclasses import replace
import unittest
from unittest.mock import AsyncMock, patch

from harnest import cron
from harnest.context import activate_context, revoke_context
from harnest.cron import CompiledCron, CronNotFoundError, CronRuntimeError
from harnest.runtime_task import TaskExecutionError, TaskRuntimeError
from harnest.runtime_task_store import _record_snapshot
from harnest.task import MemoryTaskStore, TaskRecord

from test_task_store_recovery import ControlledWorkerManager
from test_task_store_runtime import application_for, invocation


class StaticProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        """Create one real compiled declaration over a controlled reference worker."""

        async def deliver(value):
            """Expose a schedulable target without external side effects."""

            return value

        self.store = MemoryTaskStore()
        self.authored, application = application_for(deliver, self.store)
        self.schedule = CompiledCron(name="daily", source="cron/daily.py", schedule="0 8 * * *",
                                     timezone="UTC", task=application.tasks[0], arguments={"value": "daily"})
        self.application = replace(application, crons=(self.schedule,))

    async def start_manager(self, application=None):
        """Register cleanup immediately after starting a compiled deployment."""

        manager = ControlledWorkerManager(application or self.application)
        await manager.start()
        self.addAsyncCleanup(manager.close)
        return manager

    async def schedules(self):
        """Read only the automation owner's declarations through scoped storage."""

        return await self.store.list_crons(application_id=self.application.name,
                                           user_id="_harnest_automation", limit=100)

    async def test_unchanged_restart_retains_due_cursor_and_revision(self):
        """A deployment restart cannot skip an occurrence that became due offline."""

        first = await self.start_manager()
        record, = await self.schedules()
        edited = await self.store.update_cron(replace(record, next_run_at=60.0), expected_revision=record.revision)
        await first.close()
        await self.start_manager()
        restarted, = await self.schedules()
        self.assertEqual((restarted.next_run_at, restarted.revision), (60.0, edited.revision))

    async def test_retarget_pauses_old_schedule_and_preserves_immutable_target_identity(self):
        """Retargeting a declaration cannot mutate or leave its old schedule active."""

        first = await self.start_manager()
        old, = await self.schedules()
        await first.close()
        original = self.application.tasks[0]
        other = replace(original, name="harnest.provider_test.tasks.alternate")
        application = replace(self.application, tasks=(original, other),
                              crons=(replace(self.schedule, task=other),))
        await self.start_manager(application)
        records = await self.schedules()
        self.assertEqual(len(records), 2)
        retired = next(record for record in records if record.schedule_id == old.schedule_id)
        active = next(record for record in records if record.status == "active")
        self.assertEqual((retired.status, retired.task_name), ("paused", original.name))
        self.assertEqual(active.task_name, other.name)
        self.assertNotEqual(active.schedule_id, old.schedule_id)

    async def test_removed_declaration_pauses_and_readded_declaration_resumes(self):
        """Static storage follows deployed declarations while retaining history."""

        first = await self.start_manager()
        original, = await self.schedules()
        await first.close()
        removed = await self.start_manager(replace(self.application, crons=()))
        paused, = await self.schedules()
        self.assertEqual(paused.status, "paused")
        await removed.close()
        await self.start_manager()
        restored, = await self.schedules()
        self.assertEqual((restored.schedule_id, restored.status), (original.schedule_id, "active"))
        self.assertGreater(restored.revision, original.revision)

    async def test_cron_provider_errors_are_sanitized_and_missing_edits_use_public_error(self):
        """Datastore diagnostics never escape through authored cron operations."""

        manager = await self.start_manager()
        active = invocation()
        self.addCleanup(revoke_context, active)
        with activate_context(active), cron._activate_runtime(manager.cron_runtime):
            job = await cron.create(key="personal", expression="* * * * *", task=self.authored,
                                    arguments={"value": "private"})
            with patch.object(self.store, "get_cron", new=AsyncMock(side_effect=OSError("secret-dsn"))):
                with self.assertRaises(CronRuntimeError) as raised:
                    await cron.get(job.id)
            self.assertNotIn("secret-dsn", str(raised.exception))
            with patch.object(self.store, "update_cron", new=AsyncMock(side_effect=KeyError("secret-row"))):
                with self.assertRaises(CronNotFoundError) as missing:
                    await cron.pause(job.id)
            self.assertNotIn("secret-row", str(missing.exception))

    async def test_cancelled_provider_record_reports_task_cancellation(self):
        """Neutral cancelled states retain the released durable result semantics."""

        manager = await self.start_manager()
        handle = await self.authored.defer(value="private", schedule_in=60)
        self.assertTrue(await handle.cancel())
        with self.assertRaisesRegex(TaskExecutionError, "task_cancelled"):
            await handle.result()


class ProviderSnapshotTests(unittest.TestCase):
    def test_snapshot_owner_and_application_must_match_persisted_scope(self):
        """A malformed custom provider cannot restore another user's capability."""

        snapshot = dict(framework="adk", agent_name="app", invocation_id="run",
                        user_id="alice", session_id="session", metadata={})
        record = TaskRecord("job", "app", "bob", "deliver", "default", {}, invocation=snapshot)
        with self.assertRaisesRegex(TaskRuntimeError, "owner scope"):
            _record_snapshot(record, "app")
        with self.assertRaisesRegex(TaskRuntimeError, "application scope"):
            _record_snapshot(replace(record, user_id="alice"), "other-app")
        self.assertEqual(_record_snapshot(replace(record, user_id="alice"), "app")["user_id"], "alice")


if __name__ == "__main__":
    unittest.main()
