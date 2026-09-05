"""Allow bounded resource release without reviving cancelled execution authority."""

from contextvars import copy_context
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from harnest.sandbox import Sandbox, SandboxCancelledError, control


class SandboxCleanupTests(unittest.TestCase):
    def test_cancelled_parent_allows_cleanup_but_never_execution(self):
        """The narrow exception must not clear the parent's cancellation token."""
        factory = Mock()
        tool = Sandbox.provider(factory).to_langchain_tool()
        with control.execute(30) as parent:
            parent.cancelled.set()
            with control.cleanup(2) as cleanup:
                cleanup.check()
                self.assertGreater(cleanup.remaining(), 0)
                with self.assertRaises(SandboxCancelledError):
                    tool.invoke({"code": "pass"})
            self.assertIs(control.current(), parent)
            with self.assertRaises(SandboxCancelledError):
                parent.check()
        factory.assert_not_called()

    def test_cleanup_deadline_is_finite_and_nested_scopes_cannot_extend_it(self):
        clock = Mock(return_value=100.0)
        with patch("harnest.sandbox_control.time", SimpleNamespace(monotonic=clock)):
            with self.assertRaises(TimeoutError), control.cleanup(2) as outer:
                with control.cleanup(20) as inner:
                    self.assertEqual(inner.deadline, outer.deadline)
                    clock.return_value = 103
                    inner.check()
        for invalid in (None, 0, -1, True):
            with self.subTest(timeout=invalid), self.assertRaises(ValueError), control.cleanup(invalid):
                self.fail("unbounded cleanup admitted")

    def test_cleanup_lock_wait_is_bounded(self):
        lock = threading.Lock()
        lock.acquire()
        try:
            with self.assertRaises(TimeoutError), control.cleanup(1) as budget:
                # Keep the real admission-loop check fast and deterministic.
                budget.constrain(0.02)
                with budget.acquire(lock):
                    self.fail("held lock acquired")
        finally:
            lock.release()

    def test_retained_cleanup_context_is_revoked_on_return(self):
        with control.cleanup(2):
            retained = copy_context()
        with self.assertRaises(SandboxCancelledError):
            retained.run(lambda: control.current().check())
        self.assertIsNone(control.current())

    def test_cleanup_failure_restores_cancelled_parent(self):
        with control.execute(30) as parent:
            parent.cancelled.set()
            with self.assertRaisesRegex(ValueError, "remove failed"), control.cleanup(2):
                raise ValueError("remove failed")
            self.assertIs(control.current(), parent)
            self.assertTrue(parent.cancelled.is_set())
