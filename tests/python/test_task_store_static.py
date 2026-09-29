"""Verify static schedule deployment reconciliation and provider safety seams."""

from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from harnest import cron
from harnest.bundle import _discover_tasks_and_crons
from harnest.context import activate_context, revoke_context
from harnest.cron import CompiledCron, CronNotFoundError, CronRuntimeError, CronStore
from harnest.runtime_task import TaskExecutionError, TaskRuntimeError
from harnest.runtime_task_store import _record_snapshot
from harnest.task import MemoryTaskStore, TaskRecord, TaskStore
from harnest.testing import TaskStoreConformanceMixin

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

    def decorated_application(self, *, fixed):
        """Compile actual cron source without any separately authored task file."""

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "cron").mkdir()
        schedule = "'* * * * *', arguments={'value': 'retry'}, " if fixed else ""
        (root / "cron" / "report.py").write_text(
            "from harnest.cron import cron\n_attempts = 0\n"
            f"@cron({schedule}queue='reports', max_retries=2)\n"
            "async def report(value):\n"
            "    '''Return scheduled work after a transient failure.'''\n"
            "    global _attempts\n    _attempts += 1\n"
            "    if value == 'retry' and _attempts == 1:\n"
            "        raise RuntimeError('transient')\n"
            "    return {'value': value}\n",
            encoding="utf-8",
        )
        tasks, crons = _discover_tasks_and_crons(root, self.application.name)
        return replace(self.application, tasks=tasks, crons=crons)

    async def claim_report(self):
        """Claim through storage so queue selection and lease ownership are real."""

        records = await self.store.claim_tasks(
            application_id=self.application.name, queues=("reports",),
            now=time.time(), lease_seconds=30, limit=10,
        )
        self.assertEqual(len(records), 1)
        return records[0]

    async def test_decorated_static_schedule_dispatches_retries_and_survives_restart(self):
        """Implicit tasks use ordinary atomic handoff, retries and stable identities."""

        application = self.decorated_application(fixed=True)
        manager = await self.start_manager(application)
        schedule, = await self.schedules()
        due = await self.store.update_cron(
            replace(schedule, next_run_at=60), expected_revision=schedule.revision,
        )
        await manager.cron_runtime.dispatch(60)
        await manager.cron_runtime.dispatch(60)
        record = await self.claim_report()
        self.assertEqual((record.queue, record.max_retries, record.trigger), ("reports", 2, "cron"))
        await manager._execute_record(record)
        failed = await self.store.get_task(application_id=application.name, job_id=record.job_id)
        self.assertEqual(failed.status, "pending")
        with patch("time.time", return_value=failed.scheduled_at + 1):
            await manager._execute_record(await self.claim_report())
        finished = await self.store.get_task(application_id=application.name, job_id=record.job_id)
        self.assertEqual((finished.status, finished.result, finished.attempt),
                         ("completed", {"value": "retry"}, 2))
        await manager.close()
        await self.start_manager(self.decorated_application(fixed=True))
        restarted, = await self.schedules()
        self.assertEqual((restarted.schedule_id, restarted.next_run_at), (due.schedule_id, 120))

    async def test_dynamic_cron_resolves_names_validates_arguments_and_executes(self):
        """A tool can schedule deployed cron work without importing its callable."""

        application = self.decorated_application(fixed=False)
        manager = await self.start_manager(application)
        self.assertEqual(await self.schedules(), ())
        active = invocation()
        self.addCleanup(revoke_context, active)
        with activate_context(active), cron._activate_runtime(manager.cron_runtime):
            for target in ("missing", "../report", "harnest.provider_test.tasks.cron.report"):
                with self.subTest(target=target), self.assertRaisesRegex(ValueError, "not registered"):
                    await cron.create(key="report", expression="* * * * *", task=target)
            with self.assertRaisesRegex(TypeError, "task signature"):
                await cron.create(key="report", expression="* * * * *", task="report")
            self.assertEqual(await cron.list(), ())
            job = await cron.create(key="report", expression="* * * * *", task="report",
                                    arguments={"value": "dynamic"})
            replay = await cron.create(key="report", expression="* * * * *", task=application.tasks[0].authored,
                                       arguments={"value": "dynamic"})
            self.assertEqual(replay.id, job.id)
        stored = await self.store.get_cron(application_id=application.name, user_id="user-1", schedule_id=job.id)
        await self.store.update_cron(replace(stored, next_run_at=60), expected_revision=stored.revision)
        await manager.cron_runtime.dispatch(60)
        record = await self.claim_report()
        await manager._execute_record(record)
        finished = await self.store.get_task(application_id=application.name, job_id=record.job_id)
        self.assertEqual((finished.status, finished.result, finished.user_id),
                         ("completed", {"value": "dynamic"}, "user-1"))

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

        await self.start_manager()
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


class MemoryTaskStoreTests(TaskStoreConformanceMixin, unittest.IsolatedAsyncioTestCase):
    """Run the custom-provider conformance contract against the memory reference."""

    async def make_store(self):
        """Construct one independent reference provider for each behavior check."""

        return MemoryTaskStore()

    async def test_public_protocols_recognize_reference_provider(self):
        """Feature namespaces expose usable runtime structural contracts."""

        self.assertIsInstance(self.store, TaskStore)
        self.assertIsInstance(self.store, CronStore)


if __name__ == "__main__":
    unittest.main()
