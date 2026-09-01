from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harnest.application import CompiledApplication
from harnest.bundle import (
    BundleExportError,
    BundleImportError,
    _discover_tasks_and_crons,
    compile_artifact,
)
from harnest.cron import CompiledCron, Cron
from harnest.task import CompiledTask, registration_for, task


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
