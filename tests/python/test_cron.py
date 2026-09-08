from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harnest import context, cron
from harnest.application import CompiledApplication
from harnest.bundle import (
    BundleExportError,
    BundleImportError,
    _discover_tasks_and_crons,
    compile_artifact,
)
from harnest.context import activate_context, create_agent_context, revoke_context
from harnest.cron import (
    CompiledCron,
    Cron,
    CronJob,
    CronNotFoundError,
    CronUnavailableError,
    _UNSET,
    _activate_runtime,
    _matches_schedule,
)
from harnest.task import CompiledTask, registration_for, task


class _DynamicCronRuntime:
    """Model the owner-scoped runtime boundary used by the public cron API."""

    def __init__(self) -> None:
        self._next_id = 1
        self._records: dict[tuple[str, str], dict[str, object]] = {}

    async def create_dynamic_schedule(self, *, key, expression, task, arguments):
        """Create one deterministic owner-scoped record for API contract tests."""

        owner = context.user_id
        schedule_id = f"cron_{self._next_id:032x}"
        self._next_id += 1
        record: dict[str, object] = {
            "id": schedule_id,
            "key": key,
            "expression": expression,
            "task_name": "harnest.demo.tasks.deliver",
            "arguments": dict(arguments),
            "status": "active",
        }
        self._records[(owner, schedule_id)] = record
        return self._job(record)

    async def get_dynamic_schedule(self, schedule_id):
        """Return only the current owner's matching record."""

        record = self._records.get((context.user_id, schedule_id))
        return None if record is None else self._job(record)

    async def list_dynamic_schedules(self, *, after, limit):
        """Return one stable owner-scoped page in identifier order."""

        owner = context.user_id
        records = sorted(
            (
                record
                for (record_owner, _), record in self._records.items()
                if record_owner == owner
            ),
            key=lambda record: str(record["id"]),
        )
        if after is not None:
            records = [record for record in records if str(record["id"]) > after]
        return tuple(self._job(record) for record in records[:limit])

    async def update_dynamic_schedule(self, schedule_id, *, expression, arguments):
        """Update supplied fields without changing ownership or identity."""

        record = self._require(schedule_id)
        if expression is not None:
            record["expression"] = expression
        if arguments is not _UNSET:
            record["arguments"] = dict(arguments)
        return self._job(record)

    async def set_dynamic_schedule_status(self, schedule_id, status):
        """Apply public lifecycle transitions to one owned record."""

        record = self._require(schedule_id)
        record["status"] = status
        return self._job(record)

    async def delete_dynamic_schedule(self, schedule_id):
        """Remove one owned record and report whether it existed."""

        return self._records.pop((context.user_id, schedule_id), None) is not None

    def _require(self, schedule_id: str) -> dict[str, object]:
        """Raise the public not-found error rather than crossing owner scopes."""

        try:
            return self._records[(context.user_id, schedule_id)]
        except KeyError:
            raise CronNotFoundError("cron job was not found") from None

    def _job(self, record: dict[str, object]) -> CronJob:
        """Convert private mutable test state into an immutable public value."""

        return CronJob(
            id=str(record["id"]),
            key=str(record["key"]),
            expression=str(record["expression"]),
            task_name=str(record["task_name"]),
            arguments=record["arguments"],  # type: ignore[arg-type]
            status=str(record["status"]),
            _runtime=self,
        )


def _invocation(user_id: str):
    """Create a minimal managed invocation for dynamic cron tests."""

    return create_agent_context(
        framework="langgraph",
        agent_name="reporter",
        invocation_id=f"inv-{user_id}",
        user_id=user_id,
        session_id=f"session-{user_id}",
        metadata={},
        resources={},
    )


class CronAuthoringTests(unittest.TestCase):
    def test_validates_five_column_utc_schedule_and_static_arguments(self):
        @task
        def deliver(value, *, retries=1):
            """Deliver one report."""

        declaration = Cron(
            "*/5 9-17 * * 1-5",
            task=deliver,
            arguments={"value": {"private": [1, 2]}},
        )

        self.assertEqual(declaration.timezone, "UTC")
        self.assertEqual(declaration.arguments["value"]["private"], (1, 2))
        with self.assertRaises(TypeError):
            declaration.arguments["new"] = True  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "exactly five"):
            Cron("0 0 * * * *", task=deliver, arguments={"value": "x"})
        with self.assertRaisesRegex(ValueError, "out of range"):
            Cron("60 0 * * *", task=deliver, arguments={"value": "x"})
        with self.assertRaisesRegex(ValueError, "invalid cron"):
            Cron("٠ 0 * * *", task=deliver, arguments={"value": "x"})
        with self.assertRaisesRegex(ValueError, "timezone must be UTC"):
            Cron(
                "0 0 * * *",
                task=deliver,
                arguments={"value": "x"},
                timezone="Europe/London",
            )
        with self.assertRaisesRegex(TypeError, "do not match task signature"):
            Cron("0 0 * * *", task=deliver, arguments={})


class DynamicCronTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_crud_and_lifecycle_support_module_and_job_calls(self):
        """Exercise the complete public surface tools can expose conversationally."""

        @task
        async def deliver(value: str) -> None:
            """Deliver one scheduled report."""

        runtime = _DynamicCronRuntime()
        active = _invocation("user-1")
        try:
            with activate_context(active), _activate_runtime(runtime):
                job = await cron.create(
                    key="weekly-report",
                    expression="0 9 * * 1",
                    task=deliver,
                    arguments={"value": "private"},
                )
                self.assertIsInstance(job, CronJob)
                self.assertEqual(job.status, "active")
                self.assertEqual(job.arguments, {"value": "private"})
                self.assertEqual(await cron.get(job.id), job)
                self.assertEqual(await cron.list(), (job,))

                updated = await cron.update(job.id, expression="30 10 * * 2")
                self.assertEqual(updated.expression, "30 10 * * 2")
                self.assertEqual(updated.arguments, {"value": "private"})
                updated = await updated.update(arguments={"value": "revised"})
                self.assertEqual(updated.arguments, {"value": "revised"})

                paused = await cron.pause(job.id)
                self.assertEqual(paused.status, "paused")
                resumed = await paused.resume()
                self.assertEqual(resumed.status, "active")
                cancelled = await cron.cancel(job.id)
                self.assertEqual(cancelled.status, "cancelled")
                retained = await cron.get(job.id)
                self.assertIsNotNone(retained)
                assert retained is not None
                self.assertEqual(retained.status, "cancelled")

                self.assertTrue(await cancelled.delete())
                self.assertIsNone(await cron.get(job.id))
                self.assertFalse(await cron.delete(job.id))
        finally:
            revoke_context(active)

    async def test_jobs_are_scoped_to_the_current_invocation_owner(self):
        """Prevent one user from observing or mutating another user's schedules."""

        @task
        async def deliver() -> None:
            """Deliver one scheduled report."""

        runtime = _DynamicCronRuntime()
        first = _invocation("user-1")
        second = _invocation("user-2")
        try:
            with activate_context(first), _activate_runtime(runtime):
                owned = await cron.create(
                    key="daily-report", expression="0 9 * * *", task=deliver
                )
            with activate_context(second), _activate_runtime(runtime):
                self.assertIsNone(await cron.get(owned.id))
                self.assertEqual(await cron.list(), ())
                with self.assertRaises(CronNotFoundError):
                    await cron.pause(owned.id)
            with activate_context(first), _activate_runtime(runtime):
                self.assertEqual(await cron.get(owned.id), owned)
        finally:
            revoke_context(first)
            revoke_context(second)

    async def test_dynamic_access_requires_both_invocation_and_runtime_scopes(self):
        """Keep imports safe while permitting calls from managed tools and tasks."""

        runtime = _DynamicCronRuntime()
        active = _invocation("user-1")
        try:
            with self.assertRaisesRegex(CronUnavailableError, "managed Harnest"):
                await cron.list()
            with activate_context(active), self.assertRaisesRegex(
                CronUnavailableError, "cron-enabled"
            ):
                await cron.list()
            with activate_context(active), _activate_runtime(runtime):
                self.assertEqual(await cron.list(), ())
        finally:
            revoke_context(active)

    async def test_job_methods_reject_a_different_active_runtime(self):
        """Do not let a retained handle cross application runtime boundaries."""

        @task
        async def deliver() -> None:
            """Deliver one scheduled report."""

        runtime = _DynamicCronRuntime()
        active = _invocation("user-1")
        try:
            with activate_context(active), _activate_runtime(runtime):
                job = await cron.create(
                    key="daily-report", expression="0 9 * * *", task=deliver
                )
            with activate_context(active), _activate_runtime(_DynamicCronRuntime()):
                with self.assertRaisesRegex(CronUnavailableError, "different runtime"):
                    await job.pause()
        finally:
            revoke_context(active)

    async def test_validates_dynamic_inputs_before_runtime_access(self):
        """Reject unsafe identifiers, pagination, tasks, and calls at the API edge."""

        @task
        async def deliver(value: str) -> None:
            """Deliver one scheduled report."""

        runtime = _DynamicCronRuntime()
        active = _invocation("user-1")
        try:
            with activate_context(active), _activate_runtime(runtime):
                with self.assertRaisesRegex(ValueError, "schedule key"):
                    await cron.create(
                        key="bad key", expression="0 9 * * *", task=deliver,
                        arguments={"value": "x"},
                    )
                with self.assertRaisesRegex(TypeError, "@task"):
                    await cron.create(
                        key="bad-task", expression="0 9 * * *",
                        task=lambda: None,  # type: ignore[arg-type]
                    )
                with self.assertRaisesRegex(TypeError, "task signature"):
                    await cron.create(
                        key="bad-call", expression="0 9 * * *", task=deliver
                    )
                with self.assertRaisesRegex(ValueError, "schedule id"):
                    await cron.get("other-user-schedule")
                for invalid_limit in (0, 101, True):
                    with self.subTest(limit=invalid_limit), self.assertRaisesRegex(
                        ValueError, "between 1 and 100"
                    ):
                        await cron.list(limit=invalid_limit)
        finally:
            revoke_context(active)

    def test_cron_matching_uses_utc_steps_sunday_and_vixie_day_or(self):
        """Pin occurrence matching semantics independently of scheduler timing."""

        monday = int(datetime(2026, 9, 7, 9, 10, tzinfo=timezone.utc).timestamp())
        sunday = int(datetime(2026, 9, 6, 9, 0, tzinfo=timezone.utc).timestamp())
        tuesday_first = int(
            datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc).timestamp()
        )
        tuesday_eighth = int(
            datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc).timestamp()
        )
        second_of_january = int(
            datetime(2026, 1, 2, 1, 7, tzinfo=timezone.utc).timestamp()
        )

        self.assertTrue(_matches_schedule("*/5 9 * * 1-5", monday))
        self.assertFalse(_matches_schedule("*/15 9 * * 1-5", monday))
        self.assertTrue(_matches_schedule("0 9 * * 0", sunday))
        self.assertTrue(_matches_schedule("0 9 * * 7", sunday))
        # Restricted day-of-month and day-of-week use OR, matching on either.
        self.assertTrue(_matches_schedule("0 9 1 * 1", tuesday_first))
        self.assertFalse(_matches_schedule("0 9 1 * 1", tuesday_eighth))
        # Full day-of-week ranges retain croniter's wildcard-equivalent behavior.
        self.assertFalse(_matches_schedule("* * */2 * 1-7", second_of_january))


class CronCompilerTests(unittest.TestCase):
    def test_discovers_filename_export_and_resolves_task_folder_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_task(root)
            self._write(
                root / "cron" / "daily_report.py",
                "from harnest.cron import Cron\n"
                "from tasks.deliver import deliver\n\n"
                "daily_report = Cron(\n"
                "    '0 9 * * 1-5', task=deliver, arguments={'value': 'private'}\n"
                ")\n",
            )

            tasks, crons = _discover_tasks_and_crons(root, "reporter")

        self.assertEqual(len(tasks), 1)
        self.assertEqual(len(crons), 1)
        self.assertIs(crons[0].task, tasks[0])
        self.assertEqual(crons[0].name, "harnest.reporter.cron.daily_report")
        self.assertEqual(crons[0].task_name, "harnest.reporter.tasks.deliver")
        self.assertEqual(crons[0].arguments, {"value": "private"})

    def test_rejects_cron_targets_outside_discovered_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_task(root)
            self._write(
                root / "cron" / "daily_report.py",
                "from harnest.cron import Cron\n"
                "from harnest.task import task\n\n"
                "@task\n"
                "def outside(value):\n"
                "    '''Run outside the task folder.'''\n\n"
                "daily_report = Cron('0 9 * * *', task=outside, "
                "arguments={'value': 'x'})\n",
            )

            with self.assertRaisesRegex(
                BundleExportError, "root tasks/ folder"
            ):
                _discover_tasks_and_crons(root, "reporter")

    def test_rejects_invalid_arguments_during_cron_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_task(root)
            self._write(
                root / "cron" / "daily_report.py",
                "from harnest.cron import Cron\n"
                "from tasks.deliver import deliver\n\n"
                "daily_report = Cron('0 9 * * *', task=deliver)\n",
            )

            with self.assertRaisesRegex(BundleImportError, "task signature"):
                _discover_tasks_and_crons(root, "reporter")

    def test_manifest_records_schedule_without_static_arguments(self):
        @task(queue="reports", max_retries=2)
        def deliver(value):
            """Deliver one report."""

        definition = registration_for(deliver)
        assert definition is not None
        compiled_task = CompiledTask(
            name="harnest.reporter.tasks.deliver",
            source="tasks/deliver.py",
            definition=definition,
            authored=deliver,
        )
        compiled_cron = CompiledCron(
            name="harnest.reporter.cron.daily_report",
            source="cron/daily_report.py",
            schedule="0 9 * * *",
            timezone="UTC",
            task=compiled_task,
            arguments={"value": "manifest-secret"},
        )
        application = CompiledApplication(
            name="reporter",
            framework="langgraph",
            mode="managed",
            target=object(),
            tasks=(compiled_task,),
            crons=(compiled_cron,),
        )
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / "agent"
            output = workspace / "artifact"
            self._write(root / "agent.py", "root_agent = object()\n")
            self._write(root / "cron" / "daily_report.py", "schedule = 1\n")
            with patch(
                "harnest.bundle.compile_application", return_value=application
            ):
                first = compile_artifact(root, output, framework="langgraph")
            self._write(root / "cron" / "daily_report.py", "schedule = 2\n")
            with patch(
                "harnest.bundle.compile_application", return_value=application
            ):
                second = compile_artifact(root, output, framework="langgraph")
            persisted = json.loads(
                (output / "harnest-manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(first["crons"], persisted["crons"])
        self.assertEqual(first["crons"][0]["task"], compiled_task.name)
        self.assertNotIn("arguments", first["crons"][0])
        self.assertNotIn("manifest-secret", json.dumps(first))
        self.assertNotEqual(first["digest"], second["digest"])
        self.assertEqual(first["runtimeDependencies"], ["procrastinate==3.9.0"])

    @staticmethod
    def _write_task(root: Path) -> None:
        """Write one compiler-discoverable task fixture."""

        CronCompilerTests._write(
            root / "tasks" / "deliver.py",
            "from harnest.task import task\n\n"
            "@task(queue='reports')\n"
            "def deliver(value):\n"
            "    '''Deliver one report.'''\n",
        )

    @staticmethod
    def _write(path: Path, contents: str) -> None:
        """Write one authored resource with its parent directory."""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
