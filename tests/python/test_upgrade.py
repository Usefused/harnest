import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from harnest.bundle import compile_application
from harnest.cli import main as cli_main
from harnest.upgrade import UpgradeError, apply_upgrade, plan_upgrade


class RepositoryUpgradeTests(unittest.TestCase):
    def write(self, path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")

    def legacy_agent(self, root: Path, *, framework: str = "adk") -> None:
        self.write(
            root / "config.yaml",
            "apiVersion: harnest.dev/v1alpha1\n"
            "kind: Agent\n"
            "metadata:\n  name: legacy\n"
            "spec:\n"
            "  entrypoint: agent:root_agent\n"
            f"  framework:\n    name: {framework}\n    mode: managed\n"
            "  runtime:\n    version: \"3.12\"\n"
            "    requirementsFile: requirements.txt\n"
            "  resources:\n    cpu: \"1\"\n    memory: 1Gi\n",
        )
        self.write(
            root / "agent.py",
            "from harnest.agent import Agent\n"
            "root_agent = Agent(name='legacy', model='test/model')\n",
        )
        self.write(root / "instructions.md", "Legacy instructions.\n")
        self.write(
            root / "requirements.txt",
            "google-adk[eval,mcp]>=2.8,<3\nhttpx>=0.28,<1\n",
        )
        self.write(
            root / "agent-card.yaml",
            "name: Legacy\ndescription: Legacy agent.\n",
        )
        self.write(
            root / "mcp_servers" / "catalog.py",
            "from harnest.mcp import MCPClient\n\n"
            "catalog = MCPClient.streamable_http('http://catalog.test/mcp')\n",
        )
        self.write(
            root / "extensions" / "audit" / "lifecycle.py",
            "from harnest.extension import DROP_EVENT, Extension\n\n"
            "def before(context, request):\n"
            "    return request\n\n"
            "def event(context, value):\n"
            "    return DROP_EVENT if value.get('private') else value\n\n"
            "extension = Extension(\n"
            "    name='audit', before_invoke=before, on_event=event\n"
            ")\n",
        )
        self.write(
            root / "extensions" / "audit" / "adk.py",
            "from google.adk.plugins.base_plugin import BasePlugin\n"
            "extension = BasePlugin(name='audit')\n",
        )
        self.write(
            root / "extensions" / "audit" / "langgraph.py",
            "extension = object()\n",
        )

    def test_plan_is_read_only_and_lists_destructive_contract_changes(self):
        """Plan required migrations without adding optional server defaults."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.legacy_agent(root)
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }

            plan = plan_upgrade(root)

            after = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
        self.assertEqual(before, after)
        self.assertEqual(plan.blockers, ())
        operations = {(item.kind, item.path, item.destination) for item in plan.actions}
        self.assertIn(("move", "mcp_servers", "mcp"), operations)
        self.assertNotIn(("create", "server.yaml", None), operations)
        self.assertIn(("create", "harnest.lock", None), operations)
        self.assertIn(("create", "lib/storage.py", None), operations)
        self.assertIn(("create", "extensions/storage.py", None), operations)
        self.assertIn(
            ("move", "extensions/audit/langgraph.py", "extensions/audit/_langgraph.py"),
            operations,
        )

    def test_apply_backs_up_and_migrates_mcp_and_lifecycle_source(self):
        """Keep backups and finish migrations without creating redundant server YAML."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.legacy_agent(root)
            plan = plan_upgrade(root)

            backup = apply_upgrade(plan)

            self.assertIsNotNone(backup)
            self.assertTrue((backup / "plan.json").is_file())
            self.assertTrue((backup / "mcp_servers" / "catalog.py").is_file())
            self.assertFalse((root / "mcp_servers").exists())
            mcp = (root / "mcp" / "catalog.py").read_text(encoding="utf-8")
            self.assertIn("def client():", mcp)
            lifecycle = (root / "lifecycle" / "audit" / "lifecycle.py").read_text(
                encoding="utf-8"
            )
            self.assertIn("from harnest.lifecycle import DROP_EVENT, lifecycle", lifecycle)
            self.assertIn("@lifecycle.before_invoke", lifecycle)
            self.assertIn("@lifecycle.on_event", lifecycle)
            self.assertNotIn("Extension(", lifecycle)
            native = (root / "lifecycle" / "audit" / "adk.py").read_text(
                encoding="utf-8"
            )
            self.assertIn("@_harnest_lifecycle.adk_plugin", native)
            self.assertTrue((root / "lifecycle" / "audit" / "_langgraph.py").is_file())
            self.assertFalse((root / "server.yaml").exists())
            self.assertTrue((root / "harnest.lock").is_file())
            self.assertFalse((root / "requirements.txt").exists())
            pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
            self.assertIn('"httpx>=0.28,<1"', pyproject)
            self.assertNotIn("google-adk", pyproject)
            self.assertIn("dependencyFile: pyproject.toml", (root / "config.yaml").read_text(encoding="utf-8"))
            self.assertEqual(plan_upgrade(root).actions, ())
            with patch.dict(os.environ, {}, clear=False):
                application = compile_application(
                    root,
                    entrypoint="agent:root_agent",
                    framework="adk",
                    mode="managed",
                )
            self.assertEqual(
                [listener.phase for listener in application.extensions],
                ["before_invoke", "on_event"],
            )
            self.assertIs(application.session_store, application.checkpointer)

    def test_manual_blocker_prevents_every_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.legacy_agent(root)
            self.write(root / "mcp" / "existing.py", "def client(): return None\n")
            plan = plan_upgrade(root)

            with self.assertRaisesRegex(UpgradeError, "manual blockers"):
                apply_upgrade(plan)

            self.assertFalse((root / "server.yaml").exists())
            self.assertTrue((root / "mcp_servers").is_dir())

    def test_partial_storage_is_a_manual_blocker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.legacy_agent(root)
            self.write(
                root / "extensions" / "storage.py",
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.session_store\n"
                "def session_store(): return object()\n",
            )

            plan = plan_upgrade(root)

        self.assertTrue(
            any("add @checkpointer manually" in value for value in plan.blockers)
        )

    def test_new_storage_path_after_planning_invalidates_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.legacy_agent(root)
            plan = plan_upgrade(root)
            self.write(root / "lib" / "storage.py", "authored = True\n")

            with self.assertRaisesRegex(UpgradeError, "changed after planning"):
                apply_upgrade(plan)

            self.assertEqual(
                (root / "lib" / "storage.py").read_text(encoding="utf-8"),
                "authored = True\n",
            )
            self.assertFalse((root / ".harnest").exists())

    def test_unmappable_legacy_extensions_are_manual_blockers(self):
        cases = (
            (
                "extension = Extension(name='first')\n"
                "extension = Extension(name='second')\n",
                "exactly one legacy Extension",
            ),
            (
                "def before(context, request): return request\n"
                "extension = Extension('audit', before_invoke=before)\n",
                "positional Extension arguments",
            ),
        )
        for declaration, message in cases:
            with (
                self.subTest(message=message),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                self.legacy_agent(root)
                path = root / "extensions" / "audit" / "lifecycle.py"
                self.write(
                    path,
                    "from harnest.extension import Extension\n" + declaration,
                )

                plan = plan_upgrade(root)

            self.assertTrue(any(message in value for value in plan.blockers))

    def test_nested_legacy_requirements_are_a_manual_blocker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.legacy_agent(root)
            self.write(root / "requirements.txt", "-r private-requirements.txt\n")

            plan = plan_upgrade(root)

        self.assertTrue(
            any("dependency option requires manual migration" in value for value in plan.blockers)
        )

    def test_stale_plan_fails_before_creating_backup_or_rewriting_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.legacy_agent(root)
            plan = plan_upgrade(root)
            mcp_path = root / "mcp_servers" / "catalog.py"
            mcp_before = mcp_path.read_text(encoding="utf-8")
            self.write(root / "mcp_servers" / "_late-note.md", "changed\n")

            with self.assertRaisesRegex(UpgradeError, "changed after planning"):
                apply_upgrade(plan)

            self.assertEqual(mcp_path.read_text(encoding="utf-8"), mcp_before)
            self.assertFalse((root / ".harnest").exists())

    def test_older_project_lock_is_rewritten_and_backed_up(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.legacy_agent(root)
            self.write(
                root / "harnest.lock",
                "apiVersion: harnest.dev/v1alpha1\n"
                "kind: ProjectLock\n"
                "projectSchema: 0\n",
            )

            plan = plan_upgrade(root)
            lock_action = next(
                item for item in plan.actions if item.path == "harnest.lock"
            )
            backup = apply_upgrade(plan)

            self.assertEqual(lock_action.kind, "rewrite")
            self.assertIn(
                "projectSchema: 0",
                (backup / "harnest.lock").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "projectSchema: 3",
                (root / "harnest.lock").read_text(encoding="utf-8"),
            )

    def test_current_repository_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.legacy_agent(root, framework="langgraph")
            (root / "extensions" / "audit" / "adk.py").unlink()
            apply_upgrade(plan_upgrade(root))

            second = plan_upgrade(root)

        self.assertEqual(second.actions, ())
        self.assertEqual(second.blockers, ())

    def test_cli_prints_effective_plan_before_reporting_destructive_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.legacy_agent(root)
            stdout = StringIO()

            with redirect_stdout(stdout):
                result = cli_main(["upgrade", str(root), "--apply"])

        output = stdout.getvalue()
        self.assertEqual(result, 0)
        self.assertLess(output.index("plan (applying)"), output.index("Upgrade applied"))


if __name__ == "__main__":
    unittest.main()
