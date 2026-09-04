"""Keep nested helper budgets local without weakening worker cancellation."""

import asyncio
from contextvars import copy_context
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from google.adk.code_executors.code_execution_utils import CodeExecutionInput

from harnest.context import activate_context, revoke_context
from harnest.sandbox import Sandbox
from harnest.sandbox_control import SandboxCancelledError, current_control, execution_control
from harnest.sandbox_types import SandboxResult
from test_sandbox_control import _managed


class SandboxDeadlineTests(unittest.TestCase):
    """Use an isolated clock so deadline regressions require no wall-clock sleeps."""

    def setUp(self):
        """Replace only the control module's clock, leaving asyncio's clock intact."""
        self.clock = Mock(return_value=100.0)
        clock_patch = patch("harnest.sandbox_control.time", SimpleNamespace(monotonic=self.clock))
        clock_patch.start()
        self.addCleanup(clock_patch.stop)

    def test_short_helper_does_not_shorten_outer_or_reset_elapsed_time(self):
        """A successful five-second helper leaves the original sixty-second end time."""
        with execution_control(60) as outer:
            with execution_control(5) as helper:
                self.assertEqual(helper.deadline, 105)
                self.clock.return_value = 103
            self.assertIs(current_control(), outer)
            self.assertEqual(outer.deadline, 160)
            self.assertEqual(outer.remaining(), 57)
            self.clock.return_value = 110
            outer.check()
        self.assertIsNone(current_control())

    def test_unbounded_outer_stays_unbounded_after_limited_helper(self):
        with execution_control(None) as outer:
            with execution_control(5):
                pass
            self.assertIsNone(outer.deadline)
            self.assertIsNone(outer.remaining())

    def test_long_or_unbounded_helpers_cannot_extend_ancestor_deadlines(self):
        with execution_control(10) as outer:
            self.clock.return_value = 104
            for timeout in (60, None):
                with execution_control(timeout) as helper:
                    self.assertEqual(helper.deadline, 110)
                    self.assertEqual(helper.remaining(), 6)
            self.clock.return_value = 111
            with self.assertRaises(TimeoutError):
                outer.check()

    def test_retained_helper_context_keeps_short_deadline_after_scope_exit(self):
        """Restoring the parent must not grant detached helper workers extra time."""
        with execution_control(60) as outer:
            with execution_control(5):
                retained = copy_context()
            self.clock.return_value = 106
            with self.assertRaises(TimeoutError):
                retained.run(lambda: current_control().check())
            outer.check()

    def test_later_ancestor_constraint_still_limits_existing_descendants(self):
        with execution_control(60) as outer:
            with execution_control(None) as middle:
                with execution_control(30) as helper:
                    outer.constrain(2)
                    self.assertEqual(helper.deadline, 102)
                    self.assertEqual(middle.remaining(), 2)
                    self.clock.return_value = 103
                    with self.assertRaises(TimeoutError):
                        helper.check()

    def test_nested_failure_revokes_helper_but_preserves_parent(self):
        """Recovery keeps the caller healthy without reviving detached failed work."""
        with execution_control(60) as outer:
            with self.assertRaisesRegex(ValueError, "helper failed"):
                with execution_control(5) as helper:
                    retained = copy_context()
                    raise ValueError("helper failed")
            self.assertIs(current_control(), outer)
            self.assertEqual(outer.deadline, 160)
            self.assertIs(helper.cancelled, outer.cancelled)
            with self.assertRaises(SandboxCancelledError):
                retained.run(lambda: current_control().check())
            outer.check()
            self.assertFalse(outer.cancelled.is_set())
            with execution_control(5) as recovery:
                recovery.check()

    def test_expired_nested_scope_preserves_parent_budget(self):
        with execution_control(60) as outer:
            with self.assertRaises(TimeoutError):
                with execution_control(5) as helper:
                    self.clock.return_value = 106
                    helper.check()
            self.assertFalse(outer.cancelled.is_set())
            self.assertEqual(outer.deadline, 160)
            outer.check()

    def test_retained_failed_helper_cannot_cancel_healthy_parent(self):
        """Re-entering a failed worker context must not escalate its local failure."""
        def retry():
            with execution_control(5):
                self.fail("failed helper admitted more work")

        with execution_control(60) as outer:
            with self.assertRaises(ValueError):
                with execution_control(5):
                    retained = copy_context()
                    raise ValueError("missing container")
            with self.assertRaises(SandboxCancelledError):
                retained.run(retry)
            outer.check()
            self.assertFalse(outer.cancelled.is_set())

    def test_explicit_cancellation_still_cancels_parent(self):
        with execution_control(60) as outer:
            with self.assertRaises(asyncio.CancelledError):
                with execution_control(5):
                    raise asyncio.CancelledError()
            self.assertTrue(outer.cancelled.is_set())
            with self.assertRaises(SandboxCancelledError):
                outer.check()

    def test_nested_scope_retains_captured_managed_revocation(self):
        active = _managed()
        with activate_context(active), execution_control(60):
            with execution_control(5) as helper:
                retained = copy_context()
            revoke_context(active)
            with self.assertRaises(SandboxCancelledError):
                retained.run(helper.check)

    def _exercise_adapter(self, framework):
        """Run real native adapters with short provider-startup and execution helpers."""
        def execute(_request):
            """Finish transport setup before continuing beyond its shorter deadline."""
            with execution_control(5):
                self.clock.return_value = 103
            self.clock.return_value = 110
            current_control().check()
            return SandboxResult(stdout="outer work completed")

        def factory():
            """Simulate successful short startup without shrinking execution time."""
            with execution_control(1):
                pass
            self.clock.return_value = 102
            current_control().check()
            return SimpleNamespace(execute=execute)

        definition = Sandbox.provider(factory, timeout_seconds=60)
        if framework == "adk":
            result = definition.to_adk_executor().execute_code(None, CodeExecutionInput(code="pass"))
            self.assertEqual(result.stdout, "outer work completed")
        else:
            result = definition.to_langchain_tool().invoke({"code": "pass"})
            self.assertEqual(result["stdout"], "outer work completed")
        self.assertIsNone(current_control())

    def test_adk_provider_helpers_leave_time_for_outer_execution(self):
        self._exercise_adapter("adk")

    def test_langgraph_provider_helpers_leave_time_for_outer_execution(self):
        self._exercise_adapter("langgraph")

    def test_both_adapters_can_recover_from_missing_container(self):
        """Cross native adapters and runtime admission after a caught Docker error."""
        from docker.errors import NotFound

        def execute(_request):
            try:
                with execution_control(5):
                    raise NotFound("container no longer exists")
            except NotFound:
                pass
            with execution_control(5) as recovery:
                recovery.check()
            return SandboxResult(stdout="recreated")

        definition = Sandbox.provider(lambda: SimpleNamespace(execute=execute), timeout_seconds=60)
        adk_result = definition.to_adk_executor().execute_code(None, CodeExecutionInput(code="pass"))
        self.assertEqual(adk_result.stdout, "recreated")
        self.assertEqual(definition.to_langchain_tool().invoke({"code": "pass"})["stdout"], "recreated")


class ConcurrentSandboxDeadlineTests(unittest.IsolatedAsyncioTestCase):
    """Prove sibling workers keep independent limits while sharing cancellation."""

    async def test_thread_helper_does_not_tighten_parent_or_sibling(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def helper():
            """Keep a short nested scope active while another task checks its budget."""
            with execution_control(5):
                entered.set()
                await release.wait()

        with execution_control(60) as outer:
            deadline = outer.deadline
            worker = asyncio.create_task(helper())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                sibling = await asyncio.to_thread(lambda: current_control().deadline)
                self.assertEqual(sibling, deadline)
                self.assertEqual(outer.deadline, deadline)
            finally:
                release.set()
                await worker

    async def test_parent_cancellation_reaches_nested_worker(self):
        with execution_control(60) as outer:
            with execution_control(5):
                outer.cancelled.set()
                with self.assertRaises(SandboxCancelledError):
                    await asyncio.to_thread(lambda: current_control().check())
