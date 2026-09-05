"""Prevent cancelled or revoked queued sandbox work from reaching providers."""

import asyncio
from contextvars import copy_context
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from google.adk.code_executors.code_execution_utils import CodeExecutionInput
from google.adk.models import LlmResponse

from harnest.context import (
    ContextUnavailableError, activate_context, create_agent_context,
    optional_active_context, revoke_context,
)
from harnest.sandbox import Sandbox
from harnest.sandbox_adapters import execution_context
from harnest.sandbox_adk import _CodeContinuationProcessor
from harnest.sandbox_control import (
    SandboxCancelledError, SandboxControl, current_control, execution_control,
)
from harnest.sandbox_types import SandboxResult


def _managed():
    """Provide one revocable tenant identity without a native framework runtime."""
    return create_agent_context(
        framework="langgraph", agent_name="worker", invocation_id="call",
        user_id="alice", session_id="session", metadata={}, resources={},
    )


class QueuedBackend:
    """Hold admission independently of asyncio so cancellation races are observable."""

    def __init__(self):
        """Begin locked and record admission separately from actual execution."""
        self.lock = threading.Lock()
        self.lock.acquire()
        self.entered = threading.Event()
        self.finished = threading.Event()
        self.executed = False

    def execute(self, request):
        """Honor the shared control exactly as a conforming provider does."""
        self.entered.set()
        try:
            with current_control().acquire(self.lock):
                self.executed = True
                return SandboxResult(stdout="executed")
        finally:
            self.finished.set()


class SandboxControlTests(unittest.TestCase):
    """Check explicit unmanaged access, absolute deadlines, and captured lifetime."""

    def test_absent_context_is_distinct_from_revoked_identity(self):
        """Only genuine standalone calls may use native identity fallback."""
        self.assertIsNone(optional_active_context())
        self.assertIsNone(execution_context().user_id)
        native = SimpleNamespace(session=SimpleNamespace(user_id="native", id="session"))
        self.assertEqual(execution_context(native).user_id, "native")
        active = _managed()
        with activate_context(active):
            retained = copy_context()
        revoke_context(active)
        with self.assertRaises(ContextUnavailableError):
            retained.run(execution_context, native)

    def test_revoked_context_rejects_both_adapters_before_factory(self):
        """Do not build a provider after managed authority has been revoked."""
        factory = Mock()
        definition = Sandbox.provider(factory)
        tool = definition.to_langchain_tool()
        executor = definition.to_adk_executor()
        active = _managed()
        with activate_context(active):
            revoke_context(active)
            with self.assertRaises(ContextUnavailableError):
                tool.invoke({"code": "pass"})
            with self.assertRaises(ContextUnavailableError):
                executor.execute_code(None, CodeExecutionInput(code="pass"))
        factory.assert_not_called()

    def test_watchdog_checks_captured_lifetime_without_contextvars(self):
        """Revocation must remain visible in a newly created watchdog thread."""
        active = _managed()
        with activate_context(active):
            control = SandboxControl(None)
        revoke_context(active)
        failure = []

        def inspect():
            """Capture the worker exception without leaking it to unittest stderr."""
            try:
                control.check()
            except SandboxCancelledError as error:
                failure.append(error)

        thread = threading.Thread(target=inspect)
        thread.start()
        thread.join(timeout=1)
        self.assertEqual(len(failure), 1)

    def test_nested_control_preserves_absolute_deadline(self):
        """Adapter boundaries cannot restart an admission deadline."""
        with execution_control(10) as control:
            initial = control.deadline
            with execution_control(20) as nested:
                self.assertIsNot(nested, control)
                self.assertIs(nested.cancelled, control.cancelled)
                self.assertEqual(nested.deadline, initial)
        self.assertIsNone(current_control())

    def test_expired_lock_wait_never_acquires_execution_authority(self):
        """An absolute deadline includes the time waiting behind another call."""
        lock = threading.Lock()
        lock.acquire()
        control = SandboxControl(None)
        control.deadline = time.monotonic() + 0.02
        try:
            with self.assertRaises(TimeoutError), control.acquire(lock):
                self.fail("expired call entered execution")
        finally:
            lock.release()


class SandboxAsyncControlTests(unittest.IsolatedAsyncioTestCase):
    """Exercise real framework entrypoints with blocked provider admission."""

    async def _entered(self, backend):
        """Fail boundedly instead of hanging the suite if a worker never starts."""
        self.assertTrue(await asyncio.to_thread(backend.entered.wait, 2))

    async def _finished(self, backend):
        """Join the detached worker before asserting no delayed side effects."""
        self.assertTrue(await asyncio.to_thread(backend.finished.wait, 2))
        self.assertFalse(backend.executed)

    async def test_cancelled_langgraph_call_never_executes_after_unlock(self):
        """Native async cancellation revokes the worker waiting for admission."""
        backend = QueuedBackend()
        tool = Sandbox.provider(lambda: backend).to_langchain_tool()
        task = asyncio.create_task(tool.ainvoke({"code": "write_later()"}))
        try:
            await self._entered(backend)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            backend.lock.release()
        await self._finished(backend)

    async def test_revoked_queued_call_never_executes_after_unlock(self):
        """Revalidate shared lifetime after the queue releases its lock."""
        backend = QueuedBackend()
        tool = Sandbox.provider(lambda: backend).to_langchain_tool()
        active = _managed()
        with activate_context(active):
            task = asyncio.create_task(tool.ainvoke({"code": "write_later()"}))
            try:
                await self._entered(backend)
                revoke_context(active)
            finally:
                backend.lock.release()
            with self.assertRaises(SandboxCancelledError):
                await task
        await self._finished(backend)

    async def test_native_adk_processor_cancellation_revokes_worker(self):
        """Propagate native ADK generator cancellation into its to_thread worker."""
        backend = QueuedBackend()
        executor = Sandbox.provider(lambda: backend).to_adk_executor()
        invocation = SimpleNamespace(agent=SimpleNamespace(code_executor=executor))

        class NativeProcessor:
            """Use ADK's actual blocking-executor scheduling pattern."""

            async def run_async(self, context, response):
                """Wait for native execution before publishing any result event."""
                await asyncio.to_thread(executor.execute_code, context, CodeExecutionInput(code="pass"))
                yield SimpleNamespace(content=None)

        stream = _CodeContinuationProcessor(NativeProcessor()).run_async(invocation, LlmResponse())
        task = asyncio.create_task(anext(stream))
        try:
            await self._entered(backend)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            backend.lock.release()
            await stream.aclose()
        await self._finished(backend)

    async def test_cancelled_lazy_factory_wait_does_not_construct(self):
        """Queued first use must revalidate cancellation before invoking the factory."""
        from harnest.sandbox_runtime import SandboxRuntime

        factory = Mock()
        runtime = SandboxRuntime(Sandbox.provider(factory), "langgraph")
        runtime._lock.acquire()
        with execution_control(None) as control:
            worker = asyncio.create_task(asyncio.to_thread(runtime.run, lambda backend: None))
            await asyncio.sleep(0)
            control.cancelled.set()
            runtime._lock.release()
            with self.assertRaises(SandboxCancelledError):
                await worker
        factory.assert_not_called()

    async def test_adk_generator_close_revokes_inherited_worker_token(self):
        """An early event-stream close revokes workers even without task cancellation."""
        backend = QueuedBackend()
        executor = Sandbox.provider(lambda: backend).to_adk_executor()
        invocation = SimpleNamespace(agent=SimpleNamespace(code_executor=executor))
        captured = []

        class NativeProcessor:
            """Retain a worker across a yield to expose explicit aclose behavior."""

            async def run_async(self, context, response):
                """Start work with the processor token before yielding a native event."""
                captured.append(current_control())
                worker = asyncio.create_task(asyncio.to_thread(
                    executor.execute_code, context, CodeExecutionInput(code="pass"),
                ))
                try:
                    await asyncio.to_thread(backend.entered.wait, 2)
                    yield SimpleNamespace(content=None)
                    await worker
                finally:
                    worker.cancel()
                    try:
                        await worker
                    except asyncio.CancelledError:
                        pass

        stream = _CodeContinuationProcessor(NativeProcessor()).run_async(invocation, LlmResponse())
        try:
            await anext(stream)
            await stream.aclose()
            self.assertTrue(captured[0].cancelled.is_set())
        finally:
            backend.lock.release()
        await self._finished(backend)
