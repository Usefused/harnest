"""Explicit sandbox capabilities must stay private, scoped, and revocable."""

import asyncio
import threading
from dataclasses import replace
import unittest
from unittest.mock import Mock

from harnest import Agent, Sandbox, SandboxFile, SandboxResult, context
from harnest.agent_scope_adk import managed_adk_agent_type, scope_native_adk_agent
from harnest.backends.langgraph import _agent_tools
from harnest.context import (
    ContextResourceError, ContextUnavailableError, activate_agent_scope,
    activate_context, create_agent_context, revoke_context,
)
from harnest.context_sandboxes import SandboxRegistry
from harnest.sandbox_runtime import SandboxExecutionError
from test_sandbox_control import QueuedBackend


class RecordingBackend:
    """Capture requests without evaluating code or opening external resources."""
    def __init__(self):
        self.requests = []
        self.closed = 0

    def execute(self, request):
        """Return typed provider metadata independently of request metadata."""
        self.requests.append(request)
        return SandboxResult(stdout="42", metadata={"job": {"id": "synthetic"}})

    def close(self):
        """Record optional provider cleanup ownership."""
        self.closed += 1


def definition(**kwargs):
    """Construct a managed declaration without contacting a model provider."""
    return Agent(name="root", model="gemini-2.5-flash", instruction="Use authored tools.", **kwargs)


def managed(registry):
    """Allocate a fresh lifetime even when visible identity strings are equal."""
    return create_agent_context(
        framework="adk", agent_name="root", invocation_id="call",
        user_id="alice", session_id="session", metadata={}, resources={},
        sandbox_registry=registry,
    )


class SandboxCapabilityTests(unittest.IsolatedAsyncioTestCase):
    """Exercise real scoped lookup and synchronous/asynchronous entrypoints."""
    def setUp(self):
        self.backend = RecordingBackend()
        self.factory = Mock(return_value=self.backend)
        self.sandbox = Sandbox.provider(self.factory, timeout_seconds=7, metadata={"region": "eu"})
        self.registry = SandboxRegistry({"root": {"calculations": self.sandbox}, "child": {}})

    async def test_sync_and_async_preserve_policy_identity_files_and_results(self):
        """One lazy provider preserves typed files, identity, policy, and results."""
        self.factory.assert_not_called()
        with activate_context(managed(self.registry)):
            handle = context.sandboxes["calculations"]
            result = handle.execute("print(42)", input_files=(SandboxFile("in.txt", b"ok"),))
            await handle.aexecute("print(43)")
        self.factory.assert_called_once()
        self.assertIsInstance(result, SandboxResult)
        self.assertEqual(result.metadata, {"job": {"id": "synthetic"}})
        request = self.backend.requests[0]
        self.assertEqual(request.input_files[0].content, b"ok")
        self.assertEqual(request.timeout_seconds, 7)
        self.assertEqual(request.metadata, {"region": "eu"})
        self.assertEqual(request.context.agent_name, "root")
        self.assertEqual(request.context.user_id, "alice")
        self.assertEqual(request.context.session_id, "session")
        self.assertEqual(request.context.invocation_id, "call")

    async def test_unassigned_lookup_never_constructs_provider(self):
        """Lookup is an allowlist check, not model- or tool-controlled selection."""
        with activate_context(managed(self.registry)):
            with self.assertRaises(ContextResourceError):
                context.sandboxes["forbidden"]
            with activate_agent_scope("child"), self.assertRaises(ContextResourceError):
                context.sandboxes["calculations"]
        self.factory.assert_not_called()

    async def test_retained_handle_and_facade_cannot_cross_agent(self):
        """A child cannot borrow a handle captured under the parent's grants."""
        with activate_context(managed(self.registry)):
            facade = context.sandboxes
            handle = facade["calculations"]
            with activate_agent_scope("child"):
                with self.assertRaises(ContextUnavailableError):
                    facade["calculations"]
                with self.assertRaises(ContextUnavailableError):
                    handle.execute("pass")
                with self.assertRaises(ContextUnavailableError):
                    await handle.aexecute("pass")
        self.factory.assert_not_called()

    async def test_retained_handle_cannot_cross_invocation_or_application(self):
        """Matching strings cannot substitute another invocation's lifetime."""
        with activate_context(managed(self.registry)):
            handle = context.sandboxes["calculations"]
        other = SandboxRegistry({"root": {"calculations": self.sandbox}})
        for active in (managed(self.registry), managed(other)):
            with activate_context(active), self.assertRaises(ContextUnavailableError):
                handle.execute("pass")
        with self.assertRaises(ContextUnavailableError):
            handle.execute("pass")
        self.factory.assert_not_called()

    async def test_revoked_handle_never_constructs_provider(self):
        """Revoking the managed lifetime invalidates previously acquired handles."""
        active = managed(self.registry)
        with activate_context(active):
            handle = context.sandboxes["calculations"]
            revoke_context(active)
            with self.assertRaises(ContextUnavailableError):
                await handle.aexecute("pass")
        self.factory.assert_not_called()

    async def test_authority_overrides_and_invalid_inputs_fail_before_factory(self):
        """Authored API accepts code and typed files, not provider policy overrides."""
        with activate_context(managed(self.registry)):
            handle = context.sandboxes["calculations"]
            with self.assertRaises(TypeError):
                handle.execute("pass", metadata={})
            with self.assertRaises(TypeError):
                handle.execute("pass", timeout_seconds=1000)
            with self.assertRaises(ValueError):
                handle.execute(42)
            with self.assertRaises(TypeError):
                handle.execute("pass", input_files=["private-path"])
        self.factory.assert_not_called()

    async def test_cancellation_prevents_queued_execution(self):
        """Cancelling authored async work revokes worker admission after unlock."""
        backend = QueuedBackend()
        registry = SandboxRegistry({"root": {"work": Sandbox.provider(lambda: backend)}})
        with activate_context(managed(registry)):
            task = asyncio.create_task(context.sandboxes["work"].aexecute("write_later()"))
            try:
                self.assertTrue(await asyncio.to_thread(backend.entered.wait, 2))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                backend.lock.release()
            self.assertTrue(await asyncio.to_thread(backend.finished.wait, 2))
        self.assertFalse(backend.executed)

    async def test_container_input_files_error_explains_supported_alternatives(self):
        """Unsupported file transfer fails helpfully without contacting Docker."""
        from unittest.mock import patch

        registry = SandboxRegistry({"root": {"container": Sandbox.container(image="example/python")}})
        with activate_context(managed(registry)), patch("harnest.sandbox_container._create_executor") as factory:
            with self.assertRaisesRegex(SandboxExecutionError, "does not support input_files; omit"):
                context.sandboxes["container"].execute("pass", input_files=(SandboxFile("input.txt", b"ok"),))
            factory.assert_not_called()

    async def test_registry_close_is_lazy_idempotent_and_revokes_admission(self):
        """Shutdown closes used providers without constructing unused declarations."""
        unused = Mock(return_value=RecordingBackend())
        registry = SandboxRegistry({"root": {"used": self.sandbox, "unused": Sandbox.provider(unused)}})
        with activate_context(managed(registry)):
            handle = context.sandboxes["used"]
            handle.execute("pass")
            await registry.aclose()
            await registry.aclose()
            with self.assertRaises(SandboxExecutionError):
                handle.execute("pass")
        self.assertEqual(self.backend.closed, 1)
        unused.assert_not_called()

    async def test_failed_provider_cleanup_is_retained_for_retry(self):
        """A cleanup exception must not discard the only handle to the provider."""
        closer = Mock(side_effect=[RuntimeError("private-provider-detail"), None])
        self.backend.close = closer
        with activate_context(managed(self.registry)):
            context.sandboxes["calculations"].execute("pass")
        with self.assertRaisesRegex(RuntimeError, "cleanup could not be confirmed") as caught:
            await self.registry.aclose()
        self.assertNotIn("private-provider-detail", str(caught.exception))
        await self.registry.aclose()
        await self.registry.aclose()
        self.assertEqual(closer.call_count, 2)

    async def test_cleanup_waits_for_admitted_execution_without_racing_provider(self):
        """Provider cleanup cannot run concurrently with already-admitted work."""
        entered, release = threading.Event(), threading.Event()
        original = self.backend.execute

        def execute(request):
            """Hold one bounded operation while shutdown attempts to acquire it."""
            entered.set()
            release.wait(2)
            return original(request)

        self.backend.execute = execute
        with activate_context(managed(self.registry)):
            work = asyncio.create_task(context.sandboxes["calculations"].aexecute("pass"))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            closing = asyncio.create_task(self.registry.aclose())
            try:
                await asyncio.sleep(0.02)
                self.assertFalse(closing.done())
                self.assertEqual(self.backend.closed, 0)
            finally:
                release.set()
            await work
            await closing
        self.assertEqual(self.backend.closed, 1)

    async def test_native_child_denied_and_managed_scope_restored(self):
        """Both kinds of child execute in scope, but unmanaged children get no grants."""
        case = self

        class Native:
            name = "child"

            async def run_async(self, _parent):
                """Probe actual native operation scope rather than callback scope."""
                with case.assertRaises(ContextUnavailableError):
                    context.sandboxes["calculations"]
                yield context.agent_name

        native = scope_native_adk_agent(Native())
        with activate_context(managed(self.registry)):
            self.assertEqual([item async for item in native.run_async(None)], ["child"])
            self.assertEqual(context.agent_name, "root")
        self.factory.assert_not_called()

    async def test_managed_adk_scope_does_not_leak_across_yield(self):
        """Parent event consumers must not inherit the child's current identity."""
        class Native:
            name = "child"

            async def run_async(self, _parent):
                """Read actual execution scope across a generator suspension."""
                yield context.agent_name

        child = managed_adk_agent_type(Native)()
        with activate_context(managed(self.registry)):
            events = child.run_async(None)
            self.assertEqual(await anext(events), "child")
            self.assertEqual(context.agent_name, "root")
            await events.aclose()


class SandboxGrantDefinitionTests(unittest.TestCase):
    """Authored grants are immutable and never create model-visible code tools."""
    def test_named_grants_snapshot_without_model_tools(self):
        names = ["research"]
        sandbox = Sandbox.provider(Mock())
        bindings = {"research": sandbox}
        agent = definition(sandboxes=names, _sandbox_bindings=bindings)
        names.append("forbidden")
        bindings.clear()
        self.assertEqual(agent.sandboxes, ("research",))
        self.assertEqual(tuple(agent._sandbox_bindings), ("research",))
        built = agent.build()
        self.assertEqual(built.tools, [])
        self.assertIsNone(built.code_executor)
        self.assertEqual(_agent_tools(agent, ()), [])

    def test_unresolved_mixed_and_invalid_grants_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "unresolved"):
            definition(sandboxes=["research"]).build()
        with self.assertRaisesRegex(ValueError, "not both"):
            definition(sandbox=Sandbox.provider(Mock()), sandboxes=["research"])
        for names in ("research", ["research", "research"], ["../escape"], [42]):
            with self.subTest(names=names), self.assertRaises((TypeError, ValueError)):
                definition(sandboxes=names)

    def test_equivalent_grants_reuse_identity_but_conflicts_fail(self):
        from harnest.graph import Graph, Edge, START

        sandbox = Sandbox.provider(Mock())
        agent = definition(sandboxes=["research"], _sandbox_bindings={"research": sandbox})
        graph = Graph(name="workflow", nodes={"first": agent, "again": replace(agent)}, edges=(Edge(START, "first"), Edge("first", "again")))
        SandboxRegistry.from_definition(graph, "adk")
        conflicted = replace(graph, nodes={"first": agent, "again": definition()})
        with self.assertRaisesRegex(ValueError, "conflicting sandbox grants"):
            SandboxRegistry.from_definition(conflicted, "adk")

    def test_native_identity_cannot_alias_managed_grants(self):
        from google.adk.agents import LlmAgent

        sandbox = Sandbox.provider(Mock())
        native = LlmAgent(name="root", model="gemini-2.5-flash")
        root = definition(sandboxes=["research"], _sandbox_bindings={"research": sandbox}, subagents=[Agent.advanced(native)])
        with self.assertRaisesRegex(ValueError, "conflicting sandbox grants"):
            SandboxRegistry.from_definition(root, "adk")
