"""Exercise provider workers across durable wait, restart and lease failures."""

import asyncio
from dataclasses import replace
import time
import unittest
from unittest.mock import AsyncMock, patch

from harnest.approval import ApprovalRun
from harnest.checkpoint import MemoryStore, RunScope
from harnest.context import activate_context, create_agent_context, revoke_context
from harnest.durable import NativeDurableSuspended, ResumeArtifact, native_durable_call
from harnest.external_continuation import ExternalContinuationRuntime
from harnest.runtime_contract import InvocationRequest
from harnest.runtime_task_store import ProviderTaskRuntimeManager
from harnest.task import MemoryTaskStore

from test_task_store_runtime import application_for


class ControlledWorkerManager(ProviderTaskRuntimeManager):
    """Let recovery tests choose exact claims and callback failure boundaries."""

    async def _run_worker(self):
        """Keep normal startup ownership while tests drive execution explicitly."""

        await asyncio.Event().wait()


class ProviderRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        """Create real continuation ownership over a controlled provider worker."""

        self.calls = []

        async def deliver(value):
            """Record authored effects so recovery cannot silently repeat them."""

            self.calls.append(value)
            return {"report": value}

        self.tasks = MemoryTaskStore()
        self.authored, application = application_for(deliver, self.tasks)
        self.application = replace(application, framework="adk")
        self.checkpoints = MemoryStore()
        await self.checkpoints.start()
        self.addAsyncCleanup(self.checkpoints.close)
        self.continuations = ExternalContinuationRuntime(self.checkpoints, application_id=self.application.name)
        self.addAsyncCleanup(self.continuations.close)
        self.manager = ControlledWorkerManager(self.application, continuation_runtime=self.continuations)
        await self.manager.start()
        self.addAsyncCleanup(self.manager.close)
        self.request = InvocationRequest(input="report", user_id="user-1", session_id="session-1",
                                         invocation_id="run-1", metadata={}, state_delta={})
        self.run = ApprovalRun(id="run-1", user_id="user-1", session_id="session-1", call_id="run-1")
        self.artifact = ResumeArtifact("adk", "native-1", "call-1", "report")
        self.active = create_agent_context(
            framework="adk", agent_name=self.application.name, invocation_id="run-1",
            user_id="user-1", session_id="session-1", metadata={}, resources={},
        )
        self.addCleanup(revoke_context, self.active)
        await self.checkpoints.begin_run(application_id=self.application.name, user_id="user-1",
                                         session_id="session-1", run_id="run-1", framework="adk")

    async def submit_and_suspend(self):
        """Persist a user task and register a native durable result wait."""

        with activate_context(self.active):
            handle = await self.authored.defer("private", idempotency_key="report-once")
            with (self.continuations.execution(self.run, self.request),
                  native_durable_call(self.artifact), self.assertRaises(NativeDurableSuspended)):
                await handle.result()
        return handle

    async def claim(self):
        """Claim exactly one pending task through the production store contract."""

        records = await self.tasks.claim_tasks(application_id=self.application.name, queues=("default",),
                                              now=time.time(), lease_seconds=30, limit=1)
        self.assertEqual(len(records), 1)
        return records[0]

    async def test_terminal_result_reconciles_after_runtime_and_callback_restart(self):
        """Retained results complete registered waits without rerunning the effect."""

        handle = await self.submit_and_suspend()
        self.manager._application_continuations = None
        await self.manager._execute_record(await self.claim())
        waiting = await self.continuations.provider("harnest.task").lookup(handle.id)
        self.assertEqual(waiting.record.status, "pending")
        await self.manager.close()
        await self.continuations.close()
        recovered = ExternalContinuationRuntime(self.checkpoints, application_id=self.application.name)
        self.addAsyncCleanup(recovered.close)
        second = ControlledWorkerManager(self.application, continuation_runtime=recovered)
        await second.start()
        self.addAsyncCleanup(second.close)
        await second.reconcile_continuations()
        finished = await recovered.provider("harnest.task").lookup(handle.id)
        self.assertEqual(finished.record.status, "completed")
        self.assertEqual(finished.record.result, {"report": "private"})
        self.assertEqual(self.calls, ["private"])

    async def test_transport_cancellation_fences_task_and_cancels_durable_run(self):
        """The continuation cancel path applies owner scope through custom storage."""

        handle = await self.submit_and_suspend()
        await self.continuations.arm(response_id="run-1", user_id="user-1", session_id="session-1")
        self.assertFalse(await self.continuations.cancel_task_wait(
            response_id="run-1", user_id="foreign", session_id="session-1"))
        self.assertTrue(await self.continuations.cancel_task_wait(
            response_id="run-1", user_id="user-1", session_id="session-1"))
        record = await self.tasks.get_task(application_id=self.application.name, job_id=handle.id, user_id="user-1")
        self.assertEqual((record.status, record.failure_code), ("cancelled", "task_cancelled"))
        run = await self.checkpoints.get_run(scope=RunScope(self.application.name, "user-1", "session-1", "run-1"))
        self.assertEqual(run.status, "cancelled")
        self.assertEqual(self.calls, [])

    async def test_result_registration_race_publishes_completion_from_retained_record(self):
        """Re-reading after suspension registration closes the completion race."""

        with activate_context(self.active):
            handle = await self.authored.defer("private")
        claimed = await self.claim()
        original = self.manager._invocation_continuations
        manager = self.manager

        async def finish_during_registration(*args, **kwargs):
            """Commit at the boundary between the first read and registered wait."""

            suspended = await original.suspend(*args, **kwargs)
            application_port = manager._application_continuations
            manager._application_continuations = None
            try:
                await manager._execute_record(claimed)
            finally:
                manager._application_continuations = application_port
            return suspended

        from types import SimpleNamespace
        self.manager._invocation_continuations = SimpleNamespace(suspend=finish_during_registration)
        with (activate_context(self.active), self.continuations.execution(self.run, self.request),
              native_durable_call(self.artifact), self.assertRaises(NativeDurableSuspended)):
            await handle.result()
        finished = await self.continuations.provider("harnest.task").lookup(handle.id)
        self.assertEqual(finished.record.status, "completed")
        self.assertEqual(await handle.result(), {"report": "private"})
        self.assertEqual(self.calls, ["private"])

    async def test_failed_heartbeat_stops_authored_execution_without_committing_result(self):
        """Losing renewal authority cancels an attempt and preserves recoverability."""

        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def blocked_call(*args, **kwargs):
            """Observe cancellation before an authored result can be committed."""

            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with activate_context(self.active):
            handle = await self.authored.defer("private")
        record = await self.claim()
        with (patch.object(self.manager, "_call_authored", side_effect=blocked_call),
              patch.object(self.tasks, "renew_task_lease", new=AsyncMock(side_effect=ConnectionError)),
              patch("harnest.runtime_task_store._LEASE_SECONDS", 0.03)):
            worker = asyncio.create_task(self.manager._run_attempt(record))
            await asyncio.wait_for(entered.wait(), 1)
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(worker, 1)
        self.assertTrue(cancelled.is_set())
        retained = await self.tasks.get_task(application_id=self.application.name, job_id=handle.id)
        self.assertEqual(retained.status, "running")
        self.assertIsNone(retained.result)
        recovered, = await self.tasks.claim_tasks(application_id=self.application.name, queues=("default",),
                                                  now=record.lease_expires_at, lease_seconds=30, limit=1)
        self.assertNotEqual(recovered.lease_token, record.lease_token)


if __name__ == "__main__":
    unittest.main()
