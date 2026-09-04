"""Exercise generated CLI samples through the real managed compiler boundary."""

import ast
import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from harnest.bundle import compile_application, discover_evals
from harnest.plugins import release_runtime_plugins


_ROOT = Path(__file__).resolve().parents[2]


class InitExampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Build the CLI once so tests consume actual generated files."""
        cls.workspace = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.workspace.cleanup)
        cls.binary = Path(cls.workspace.name) / "harnest"
        environment = dict(os.environ, GOCACHE=str(_ROOT / ".cache" / "go-build"))
        subprocess.run(
            ["go", "build", "-o", str(cls.binary), "./cmd/harnest"],
            cwd=_ROOT, env=environment, check=True, capture_output=True, timeout=120,
        )

    def _scaffold(self, framework):
        """Create a fresh project with only the explicit example flag enabled."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "sample-agent"
        subprocess.run(
            [str(self.binary), "init", str(root), "--framework", framework, "--example"],
            check=True, capture_output=True, timeout=30,
        )
        return root

    def test_samples_are_valid_source_but_never_imported_by_default(self):
        """Ignored examples cannot activate tools, tasks, plugins, or evals."""
        for framework in ("adk", "langgraph"):
            with self.subTest(framework=framework):
                root = self._scaffold(framework)
                self._validate_and_poison_samples(root)
                application = compile_application(root, entrypoint="agent:root_agent", framework=framework)
                self.assertEqual(application.kind, "agent")
                self.assertEqual(application.tasks, ())
                self.assertEqual(application.crons, ())
                self.assertEqual(application.plugins, ())
                self.assertEqual(discover_evals(root / "agent.py").eval_sets, ())

    def _validate_and_poison_samples(self, root):
        """Check every Python template, then make accidental imports fail loudly."""
        for path in root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            ast.parse(source, filename=str(path))
            # Native-format plugin directories are ignored as a whole, too.
            if any(part.startswith("_") for part in path.relative_to(root).parts):
                path.write_text(source + '\nraise AssertionError("ignored sample imported")\n', encoding="utf-8")
        sample = json.loads((root / "evals" / "_example.evalset.json").read_text())
        self.assertEqual(sample["eval_set_id"], "starter")

    def test_renamed_samples_satisfy_the_managed_contract(self):
        """Activating sample paths produces real capabilities on both backends."""
        for framework in ("adk", "langgraph"):
            with self.subTest(framework=framework):
                root = self._scaffold(framework)
                self._activate_samples(root)
                application = compile_application(root, entrypoint="agent:root_agent", framework=framework)
                try:
                    self._assert_activated_samples(root, application)
                finally:
                    # Release before the next subtest compiles another project
                    # with the same process-owned plugin namespace.
                    release_runtime_plugins(tuple(plugin.descriptor for plugin in application.plugins))

    def _assert_activated_samples(self, root, application):
        """Check actual task linkage and runtime-plugin/eval discovery."""
        self.assertEqual(len(application.plugins), 1)
        self.assertEqual(len(application.tasks), 1)
        self.assertEqual(len(application.crons), 1)
        self.assertIs(application.crons[0].task, application.tasks[0])
        self.assertEqual(
            asyncio.run(application.tasks[0].authored(subject="daily")),
            {"subject": "daily", "status": "ready"},
        )
        self.assertEqual(len(discover_evals(root / "agent.py").eval_sets), 1)

    def _activate_samples(self, root):
        """Use the same filenames documented by the generated template headers."""
        paths = {
            "tools/_example.py": "tools/echo.py",
            "tasks/_example.py": "tasks/prepare_report.py",
            "cron/_example.py": "cron/daily_report.py",
            "extensions/_example": "extensions/starter_runtime",
            "skills/_example": "skills/getting-started",
            "evals/_example.evalset.json": "evals/starter.evalset.json",
            "sandbox/_example.py": "sandbox/calculations.py",
        }
        for source, destination in paths.items():
            (root / source).rename(root / destination)
        # Renaming registers a definition; an explicit user grant enables it.
        agent = root / "agent.py"
        agent.write_text(
            agent.read_text(encoding="utf-8")
            + "\nfrom dataclasses import replace\n"
            + "root_agent = replace(root_agent, sandboxes=['calculations'])\n",
            encoding="utf-8",
        )
