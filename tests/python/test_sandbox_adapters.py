"""Offline coverage for portable sandbox contracts and native adapters."""

import asyncio
import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from google.adk.code_executors import BaseCodeExecutor
from google.adk.code_executors.code_execution_utils import CodeExecutionInput, File

from harnest.context import activate_context, create_agent_context
from harnest.agent import Agent
from harnest.backends.langgraph import _agent_tools
from harnest.sandbox import (
    Sandbox,
    SandboxNetworkMode,
    SandboxNetworkPolicy,
    SandboxProviderCapabilities,
)
from harnest.sandbox_runtime import SandboxExecutionError
from harnest.sandbox_types import SandboxFile, SandboxResult


class RecordingBackend:
    """Record requests without evaluating the model's code on the host."""

    def __init__(self, result=None):
        """Retain a deterministic provider result and its incoming requests."""
        self.requests = []
        self.result = result or SandboxResult(stdout="42")

    def execute(self, request):
        """Return fixed output while retaining all portable request fields."""
        self.requests.append(request)
        return self.result


class LegacyExecutor(BaseCodeExecutor):
    """Expose an ADK-only implementation that must never run in LangGraph."""

    def execute_code(self, invocation_context, code_execution_input):
        """Fail if a non-ADK adapter attempts to manufacture native context."""
        raise AssertionError("native executor must not be called")


class SandboxAdapterTests(unittest.TestCase):
    """Verify providers remain lazy, portable, scoped, and privacy-safe."""

    def _context(self, framework="langgraph"):
        """Create a managed identity independent of either native framework."""
        return create_agent_context(
            framework=framework, agent_name="researcher", invocation_id="call-1",
            user_id="alice", session_id="session-1", metadata={}, resources={},
        )

    def test_adk_translates_files_identity_deadline_and_metadata(self):
        """The native code executor receives a lossless portable request."""
        backend = RecordingBackend(SandboxResult(
            stdout="42", stderr="", output_files=(SandboxFile("out.txt", b"ok"),),
        ))
        policy = SandboxNetworkPolicy.allowlist("example.com", ports=(443,))
        backend.sandbox_capabilities = SandboxProviderCapabilities(
            network_modes=frozenset({SandboxNetworkMode.ALLOWLIST}),
            host_allowlist=True,
            port_allowlist=True,
            private_network_blocking=True,
        )
        factory = Mock(return_value=backend)
        sandbox = Sandbox.provider(
            factory,
            timeout_seconds=9,
            metadata={"region": "eu"},
            network_policy=policy,
        )
        executor = sandbox.to_adk_executor()
        factory.assert_not_called()
        native = SimpleNamespace(
            agent=SimpleNamespace(name="native-agent"), invocation_id="native-call",
            session=SimpleNamespace(user_id="native-user", id="native-session"),
        )
        result = executor.execute_code(native, CodeExecutionInput(
            code="print(42)", input_files=[File("in.txt", b"input")],
            execution_id="execution-1",
        ))
        request = backend.requests[0]
        self.assertEqual(request.code, "print(42)")
        self.assertEqual(request.timeout_seconds, 9)
        self.assertEqual(request.execution_id, "execution-1")
        self.assertEqual(request.context.user_id, "native-user")
        self.assertEqual(request.context.session_id, "native-session")
        self.assertEqual(request.context.agent_name, "native-agent")
        self.assertEqual(request.context.invocation_id, "native-call")
        self.assertEqual(request.metadata, {"region": "eu"})
        self.assertIs(request.network_policy, policy)
        self.assertEqual(request.input_files[0].content, b"input")
        self.assertEqual(result.output_files[0].content, b"ok")
        self.assertEqual(result.stdout, "42")

    def test_langgraph_sync_async_share_lazy_backend_and_managed_context(self):
        """Both tool entrypoints retain Harnest identity across worker threads."""
        backend = RecordingBackend(SandboxResult(
            stdout="42", output_files=(SandboxFile("out.bin", b"binary"),),
            metadata={"provider": {"job": "job-1"}},
        ))
        factory = Mock(return_value=backend)
        tool = Sandbox.provider(factory, metadata={"region": "eu"}).to_langchain_tool()
        self.assertEqual(tool.name, "harnest_execute_python")
        factory.assert_not_called()
        with activate_context(self._context()):
            first = tool.invoke({"code": "print(42)"})
            second = asyncio.run(tool.ainvoke({"code": "print(43)"}))
        factory.assert_called_once()
        self.assertEqual(first, second)
        self.assertEqual(first["metadata"], {"provider": {"job": "job-1"}})
        self.assertEqual(first["output_files"][0]["content"], base64.b64encode(b"binary").decode())
        self.assertEqual(len(backend.requests), 2)
        for request in backend.requests:
            self.assertEqual(request.context.agent_name, "researcher")
            self.assertEqual(request.context.user_id, "alice")
            self.assertEqual(request.context.session_id, "session-1")
            self.assertEqual(request.context.invocation_id, "call-1")
            self.assertEqual(request.metadata, {"region": "eu"})

    def test_managed_context_takes_precedence_over_native_adk_identity(self):
        """Nested Harnest agent scopes must not fall back to root native identity."""
        backend = RecordingBackend()
        executor = Sandbox.provider(lambda: backend).to_adk_executor()
        with activate_context(self._context("adk")):
            executor.execute_code(SimpleNamespace(), CodeExecutionInput(code="print(42)"))
        self.assertEqual(backend.requests[0].context.agent_name, "researcher")

    def test_adk_preserves_metadata_in_native_result_and_model_output(self):
        """ADK's text-only code-result part must not silently drop provider data."""
        backend = RecordingBackend(SandboxResult(stdout="42", metadata={"job": "job-1"}))
        executor = Sandbox.provider(lambda: backend).to_adk_executor()
        result = executor.execute_code(None, CodeExecutionInput(code="print(42)"))
        self.assertEqual(result.metadata, {"job": "job-1"})
        self.assertEqual(json.loads(result.stdout), {"stdout": "42", "metadata": {"job": "job-1"}})

    def test_adk_error_metadata_does_not_replace_native_stderr(self):
        """Native error outcomes and retry decisions retain their original stderr."""
        backend = RecordingBackend(SandboxResult(stderr="syntax error", metadata={"job": "job-1"}))
        executor = Sandbox.provider(lambda: backend).to_adk_executor()
        result = executor.execute_code(None, CodeExecutionInput(code="invalid"))
        self.assertEqual(result.stderr, "syntax error")
        self.assertEqual(result.metadata, {"job": "job-1"})

    def test_tool_rejects_model_authority_fields_before_provider_creation(self):
        """Models cannot override session identity, metadata, or provider policy."""
        factory = Mock(return_value=RecordingBackend())
        tool = Sandbox.provider(factory).to_langchain_tool()
        for arguments in (
            {"code": 42},
            {"code": "ok", "metadata": {}},
            {"code": "ok", "network_policy": {}},
            {"code": "ok", "user_id": "bob"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                tool.invoke(arguments)
        factory.assert_not_called()

    def test_legacy_adk_backend_is_not_called_by_langgraph(self):
        """ADK-only providers fail clearly instead of receiving fake context."""
        tool = Sandbox.provider(LegacyExecutor).to_langchain_tool()
        with self.assertRaisesRegex(SandboxExecutionError, "SandboxBackend.execute"):
            tool.invoke({"code": "print(42)"})

    def test_invalid_provider_results_are_rejected(self):
        """Provider return values must satisfy the typed transport boundary."""
        backend = RecordingBackend()
        backend.result = {"stdout": "untyped private result"}
        tool = Sandbox.provider(lambda: backend).to_langchain_tool()
        with self.assertRaises(SandboxExecutionError) as caught:
            tool.invoke({"code": "print(42)"})
        self.assertNotIn("untyped private result", str(caught.exception))

    def test_provider_failures_emit_only_safe_audit_labels(self):
        """Exceptions and audit records must never expose code or credentials."""
        secret = "private-token-and-provider-payload"
        factory = Mock(side_effect=RuntimeError(secret))
        tool = Sandbox.provider(factory, name=secret, metadata={"token": secret}).to_langchain_tool()
        with patch("harnest.sandbox_runtime._AUDIT") as audit:
            with self.assertRaises(SandboxExecutionError) as caught:
                tool.invoke({"code": secret})
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, repr(audit.mock_calls))
        self.assertEqual(audit.info.call_args.kwargs["outcome"], "failure")
        self.assertEqual(audit.info.call_args.kwargs["backend"], "provider")

    def test_stderr_emits_failure_audit_without_logging_result(self):
        """A provider-reported execution failure has a failure audit outcome."""
        backend = RecordingBackend(SandboxResult(stderr="private syntax error", status="failed", exit_code=1))
        tool = Sandbox.provider(lambda: backend).to_langchain_tool()
        with patch("harnest.sandbox_runtime._AUDIT") as audit:
            result = tool.invoke({"code": "invalid"})
        self.assertEqual(result["stderr"], "private syntax error")
        self.assertEqual(audit.info.call_args.kwargs["outcome"], "failure")
        self.assertNotIn("private syntax error", repr(audit.mock_calls))

    def test_mcp_materialization_preserves_lazy_sandbox_tool(self):
        """Discovered MCP capabilities coexist with the compiler-owned sandbox."""
        from langchain_core.tools import StructuredTool

        factory = Mock(return_value=RecordingBackend())
        definition = Agent(name="worker", model="test/model", sandboxes=["work"], _sandbox_bindings={"work": Sandbox.provider(factory)})
        remote = StructuredTool.from_function(
            func=lambda value: value, name="remote_echo", description="Echo remote input.",
        )
        tools = _agent_tools(definition, (remote,))
        self.assertEqual([tool.name for tool in tools], ["remote_echo"])
        factory.assert_not_called()

    def test_removed_singular_sandbox_has_no_implicit_execution_tool(self):
        """A deprecated constructor argument cannot silently grant model execution."""
        with self.assertRaisesRegex(TypeError, "unexpected keyword argument.*sandbox"):
            Agent(name="worker", model="test/model", sandbox=Sandbox.provider(RecordingBackend))

    def test_adk_custom_services_retain_native_ephemeral_artifact_service(self):
        """Selecting Runner for sessions or credentials must not break code results."""
        from google.adk.artifacts import InMemoryArtifactService
        from google.adk.sessions import InMemorySessionService
        from harnest.runtime_adk import _build_adk_runner

        service = InMemorySessionService()
        application = SimpleNamespace(native_app=SimpleNamespace(name="root"))
        for sessions, credentials in ((service, None), (None, object())):
            with self.subTest(custom_sessions=sessions is not None):
                runner = Mock()
                _build_adk_runner(application, sessions, credentials, Mock(), runner)
                options = runner.call_args.kwargs
                self.assertIsInstance(options["artifact_service"], InMemoryArtifactService)
                self.assertIsInstance(options["session_service"], InMemorySessionService)
                self.assertIs(options["credential_service"], credentials)
                if sessions is not None:
                    self.assertIs(options["session_service"], sessions)


if __name__ == "__main__":
    unittest.main()
