import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from google.adk.code_executors import BaseCodeExecutor

from harnest.agent import AgentDefinition
from harnest.bundle import (
    BundleConventionError,
    BundleDuplicateError,
    BundleExportError,
    compile_agent,
)
from harnest.sandbox import Sandbox
from _session_store_fixture import write_session_store


class _RecordingExecutor(BaseCodeExecutor):
    def execute_code(self, invocation_context, code_execution_input):
        return (invocation_context, code_execution_input)


class SandboxTests(unittest.TestCase):
    def _write(self, path: Path, source: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    def _root(self, directory: str, assignments=()) -> Path:
        """Author explicit per-agent grants instead of relying on folder presence."""
        root = Path(directory)
        write_session_store(root)
        self._write(
            root / "agent.py",
            "from harnest.agent import Agent\n"
            f"root_agent = Agent(name='root', model='test/model', sandboxes={list(assignments)!r})\n",
        )
        self._write(root / "instructions.md", "Use isolated execution when needed.\n")
        return root

    def _definition(self, root: Path) -> AgentDefinition:
        with patch.object(AgentDefinition, "build", lambda definition: definition):
            return compile_agent(root)

    def test_provider_backend_is_lazy_and_validated(self):
        calls = []

        def factory():
            calls.append("built")
            return _RecordingExecutor()

        definition = Sandbox.provider(factory, name="recording", timeout_seconds=7)
        executor = definition.to_adk_executor()
        self.assertEqual(calls, [])
        self.assertEqual(executor.timeout_seconds, 7)
        self.assertEqual(executor.execute_code("context", "input"), ("context", "input"))
        self.assertEqual(calls, ["built"])
        executor.execute_code("second", "input")
        self.assertEqual(calls, ["built"])

        invalid = Sandbox.provider(lambda: object(), name="invalid")
        with self.assertRaisesRegex(TypeError, "SandboxBackend or ADK BaseCodeExecutor"):
            invalid.build()

    def test_container_contract_is_secure_by_default(self):
        """Retain explicit native network/deadline policy without starting Docker."""
        sandbox = Sandbox.container(image="example/sandbox:latest")
        self.assertEqual(sandbox.backend, "container")
        self.assertEqual(sandbox.timeout_seconds, 300)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            Sandbox.container()
        with self.assertRaisesRegex(ValueError, "exactly one"):
            Sandbox.container(image="one", docker_path="Dockerfile")
        with self.assertRaisesRegex(TypeError, "network must be a boolean"):
            Sandbox.container(image="one", network="yes")
        with self.assertRaisesRegex(ValueError, "unsupported container sandbox options"):
            Sandbox.container(image="one", options={"network_enabled": True})

    def test_native_parsing_options_reach_adk_adapter(self):
        """ADK reads retry and delimiter settings from the adapter itself."""
        definition = Sandbox.container(
            image="example/python", options={
                "error_retry_attempts": 3,
                "code_block_delimiters": [("<python>", "</python>")],
            },
        )
        executor = definition.to_adk_executor()
        self.assertEqual(executor.error_retry_attempts, 3)
        self.assertEqual(executor.code_block_delimiters, [("<python>", "</python>")])

    def test_filesystem_sandbox_is_discovered_and_empty_folder_is_optional(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            self._write(root / "sandbox" / "_README.md", "Optional sandbox.\n")
            self.assertFalse(hasattr(self._definition(root), "sandbox"))

            self._write(
                root / "sandbox" / "sandbox.py",
                "from harnest.sandbox import Sandbox\n"
                "sandbox = Sandbox.provider(lambda: None, name='test-provider')\n",
            )
            definition = self._definition(root)
            self.assertFalse(hasattr(definition, "sandbox"))
            self.assertEqual(dict(definition._sandbox_bindings), {})
            self._write(root / "agent.py", "from harnest import Agent\nroot_agent = Agent(name='root', model='test/model', sandboxes=['sandbox'])\n")
            definition = self._definition(root)
            self.assertEqual(definition._sandbox_bindings["sandbox"].backend, "test-provider")

    def test_compiling_agent_does_not_start_sandbox_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory, ["sandbox"])
            self._write(
                root / "sandbox" / "sandbox.py",
                "from harnest.sandbox import Sandbox\n"
                "def fail_if_started():\n"
                "    raise AssertionError('sandbox started during compilation')\n"
                "sandbox = Sandbox.provider(\n"
                "    fail_if_started, name='lazy-provider', timeout_seconds=5\n"
                ")\n",
            )
            compiled = compile_agent(root)

        self.assertIsNone(compiled.code_executor)
        self.assertNotIn("harnest_sandbox_sandbox", [tool.name for tool in compiled.tools])

    def test_sandbox_exports_and_duplicates_are_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            self._write(
                root / "sandbox" / "wrong.py",
                "from harnest.sandbox import Sandbox\n"
                "wrong = Sandbox.provider(lambda: None)\n",
            )
            self.assertEqual(dict(self._definition(root)._sandbox_bindings), {})

        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            self._write(
                root / "sandbox" / "sandbox.py",
                "sandbox = object()\n",
            )
            with self.assertRaisesRegex(BundleExportError, "must export Sandbox"):
                self._definition(root)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(
                root / "agent.py",
                "from harnest.agent import Agent\n"
                "from harnest.sandbox import Sandbox\n"
                "root_agent = Agent(\n"
                "    name='root', model='test/model',\n"
                "    sandbox=Sandbox.provider(lambda: None),\n"
                ")\n",
            )
            self._write(root / "instructions.md", "Use isolation.\n")
            write_session_store(root)
            self._write(
                root / "sandbox" / "sandbox.py",
                "from harnest.sandbox import Sandbox\n"
                "sandbox = Sandbox.provider(lambda: None)\n",
            )
            with self.assertRaisesRegex(Exception, "unexpected keyword argument.*sandbox"):
                self._definition(root)


if __name__ == "__main__":
    unittest.main()
