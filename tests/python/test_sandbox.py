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
from harnest.sandbox import (
    Sandbox,
    SandboxBudget,
    SandboxNetworkMode,
    SandboxNetworkPolicy,
    SandboxPolicyUnsupportedError,
    SandboxProviderCapabilities,
    SandboxResult,
    SandboxStatus,
)
from _session_store_fixture import write_session_store


class _RecordingExecutor(BaseCodeExecutor):
    def execute_code(self, invocation_context, code_execution_input):
        return (invocation_context, code_execution_input)


class _PolicyProvider:
    """Record requests while declaring only the controls exercised by tests."""

    sandbox_capabilities = SandboxProviderCapabilities(
        network_modes=frozenset(
            {SandboxNetworkMode.NONE, SandboxNetworkMode.ALLOWLIST}
        ),
        host_allowlist=True,
        port_allowlist=True,
        private_network_blocking=True,
    )

    def __init__(self):
        self.requests = []

    def execute(self, request):
        """Return a typed result after retaining the immutable request."""
        self.requests.append(request)
        return SandboxResult(stdout="ok")


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

    def test_portable_provider_enforces_and_receives_network_policy(self):
        """Validate policy capability before forwarding it below agent code."""
        provider = _PolicyProvider()
        policy = SandboxNetworkPolicy.allowlist(
            "Playwright.dev.", "203.0.113.8", ports=(443, 443),
        )
        definition = Sandbox.provider(
            lambda: provider, name="policy", network_policy=policy,
        )

        result = definition.to_langchain_tool().invoke({"code": "print('ok')"})

        self.assertEqual(result["stdout"], "ok")
        self.assertIs(provider.requests[0].network_policy, policy)
        self.assertEqual(policy.allow_hosts, ("playwright.dev", "203.0.113.8"))
        self.assertEqual(policy.allow_ports, (443,))

    def test_network_policy_fails_closed_on_unsupported_provider(self):
        """Never treat an application-level URL check as provider enforcement."""
        policy = SandboxNetworkPolicy.allowlist("example.com", ports=(443,))
        missing = Sandbox.provider(
            lambda: type(
                "MissingProvider",
                (),
                {"execute": lambda self, request: SandboxResult()},
            )(),
            network_policy=policy,
        )
        with self.assertRaisesRegex(
            SandboxPolicyUnsupportedError, "must declare SandboxProviderCapabilities",
        ):
            missing.build()

        native = Sandbox.provider(lambda: _RecordingExecutor(), network_policy=policy)
        with self.assertRaisesRegex(
            SandboxPolicyUnsupportedError, "native ADK.*cannot declare",
        ):
            native.build()

        class PartialProvider:
            sandbox_capabilities = SandboxProviderCapabilities(
                network_modes=frozenset({SandboxNetworkMode.ALLOWLIST}),
                host_allowlist=True,
            )

            def execute(self, request):
                return SandboxResult()

        unsupported = Sandbox.provider(lambda: PartialProvider(), network_policy=policy)
        with self.assertRaisesRegex(
            SandboxPolicyUnsupportedError, "destination port allowlists",
        ):
            unsupported.build()

    def test_network_policy_rejects_ambiguous_allowlist_entries(self):
        """Keep provider rules exact and independent of URL-parser behavior."""
        with self.assertRaisesRegex(ValueError, "exact hosts"):
            SandboxNetworkPolicy.allowlist("https://example.com")
        with self.assertRaisesRegex(ValueError, "requires allow_hosts"):
            SandboxNetworkPolicy(mode="allowlist")
        with self.assertRaisesRegex(ValueError, "integers from 1 to 65535"):
            SandboxNetworkPolicy.allowlist("example.com", ports=(True,))
        with self.assertRaisesRegex(ValueError, "exact hosts"):
            SandboxNetworkPolicy.allowlist("fe80::1%en0")

    def test_network_policy_keeps_direct_sandbox_construction_compatible(self):
        """Adding policy cannot reinterpret the existing positional options field."""
        options = {"error_retry_attempts": 2}
        definition = Sandbox(lambda: _RecordingExecutor(), "legacy", 7, {}, options)

        self.assertIsNone(definition.network_policy)
        self.assertEqual(definition._executor_options, options)

    def test_portable_budget_rejects_unbounded_values(self):
        """Providers receive finite budget values without core SDK translation."""
        budget = SandboxBudget(
            cpu=0.5,
            memory_bytes=64 * 1024 * 1024,
            pids=16,
            scratch_bytes=4096,
        )
        self.assertEqual(budget.cpu, 0.5)
        for values in (
            {"cpu": 0},
            {"cpu": True},
            {"cpu": float("inf")},
            {"memory_bytes": 0},
            {"pids": True},
            {"scratch_bytes": -1},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                SandboxBudget(**values)

    def test_result_status_is_independent_of_stderr(self):
        """Warnings remain successful while nonzero exits require failure status."""
        result = SandboxResult(stderr="warning", exit_code=0)
        self.assertEqual(result.status, SandboxStatus.SUCCEEDED)
        with self.assertRaises(ValueError):
            SandboxResult(exit_code=2)
        failed = SandboxResult(status="failed", exit_code=2)
        self.assertEqual(failed.status, SandboxStatus.FAILED)

    def test_native_parsing_options_reach_adk_adapter(self):
        """Extensions can configure ADK parsing without private constructor fields."""
        definition = Sandbox.provider(
            lambda: _RecordingExecutor(),
            adapter_options={
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
