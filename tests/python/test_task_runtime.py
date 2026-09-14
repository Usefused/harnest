from __future__ import annotations

import asyncio
from dataclasses import replace
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from harnest.application import CompiledApplication
from harnest import cron
from harnest.context import activate_context, create_agent_context, revoke_context
from harnest.durable import ResumeArtifact, native_durable_call
from harnest.runtime_task import TaskRuntimeError, TaskExecutionError
from harnest.runtime_task_store import ProviderTaskRuntimeManager
from harnest.runtime_contract import InvocationResult
from harnest.task import CompiledTask, MemoryTaskStore, TaskUnavailableError, registration_for, task


class _Driver:
    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self.info = SimpleNamespace()

    async def start(self):
        self.started = True

    async def close(self):
        self.closed = True


class _CronCallingDriver(_Driver):
    """Call public cron APIs as a framework tool would during an invocation."""

    def __init__(self, target) -> None:
        super().__init__()
        self.target = target

    async def invoke(self, request):
        """Create a cron job while the inner framework owns invocation context."""

        active = _cron_invocation(request)
        try:
            with activate_context(active):
                job = await cron.create(
                    key="agent-created",
                    expression="0 9 * * 1-5",
                    task=self.target,
                    arguments={"value": "from-tool"},
                )
        finally:
            revoke_context(active)
        return InvocationResult(
            text=job.id,
            events=(),
            result=job,
            session_id=request.session_id,
            metadata={},
        )

    async def stream(self, request):
        """List cron jobs while the streamed invocation context remains active."""

        active = _cron_invocation(request)
        try:
            with activate_context(active):
                jobs = await cron.list()
                yield {"type": "cron", "count": len(jobs)}
        finally:
            revoke_context(active)


def _cron_invocation(request):
    """Build the managed context normally provided by a framework driver."""

    return create_agent_context(
        framework="langgraph",
        agent_name="reporter",
        invocation_id=request.invocation_id,
        user_id=request.user_id,
        session_id=request.session_id,
        metadata=request.metadata,
        resources={},
    )


def _compiled(function, *, name="send_report", retries=3):
    decorated = task(queue="reports", max_retries=retries)(function)
    definition = registration_for(decorated)
    assert definition is not None
    compiled = CompiledTask(
        name=f"harnest.demo.tasks.{name}",
        source=f"tasks/{name}.py",
        definition=definition,
        authored=decorated,
    )
    application = CompiledApplication(
        name="demo",
        framework="langgraph",
        mode="managed",
        target=object(),
        tasks=(compiled,),
    )
    return decorated, compiled, application


class _PausedWorker(ProviderTaskRuntimeManager):
    async def _run_worker(self):
        """Let tests advance real storage claims without racing a polling loop."""
        await asyncio.Event().wait()


class TaskRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def manager_for(self, function, *, retries=3):
        """Bind a real reference provider while retaining deterministic execution."""
        authored, _, application = _compiled(function, retries=retries)
        store = MemoryTaskStore()
        application = replace(application, task_store=store, cron_store=store)
        manager = _PausedWorker(application)
        await manager.start()
        self.addAsyncCleanup(manager.close)
        return authored, manager, store

    async def execute_next(self, manager, store, *, delay=0):
        """Claim through the provider contract, then run the production attempt."""
        records = await store.claim_tasks(
            application_id="demo", queues=("reports",), now=time.time() + delay,
            lease_seconds=30, limit=1,
        )
        self.assertEqual(len(records), 1)
        await manager._execute_record(records[0])

    async def test_native_replay_deduplicates_after_payload_cleanup(self):
        async def deliver(value):
            """Return the value once for repeated durable tool execution."""
            return value

        authored, manager, store = await self.manager_for(deliver)
        active = _cron_invocation(SimpleNamespace(
            invocation_id="run-1", user_id="owner", session_id="session", metadata={},
        ))
        self.addCleanup(revoke_context, active)
        artifact = ResumeArtifact("langgraph", "thread", "call", "report")
        with activate_context(active), native_durable_call(artifact):
            first = await authored.defer("same")
        await self.execute_next(manager, store)
        with activate_context(active), native_durable_call(artifact):
            replay = await authored.defer("same")
            distinct = await authored.defer("different")
        self.assertEqual(first.id, replay.id)
        self.assertNotEqual(first.id, distinct.id)

    async def test_retry_retains_payload_then_terminal_failure_scrubs_it(self):
        async def fail(value):
            """Fail with sensitive text that must not escape task boundaries."""
            raise RuntimeError(value)

        authored, manager, store = await self.manager_for(fail, retries=1)
        handle = await authored.defer("private-value")
        await self.execute_next(manager, store)
        retained = await store.get_task(application_id="demo", job_id=handle.id)
        self.assertEqual(retained.status, "pending")
        self.assertEqual(retained.arguments, {"value": "private-value"})
        with self.assertRaisesRegex(TaskUnavailableError, "durable"):
            await handle.result()
        await self.execute_next(manager, store, delay=3)
        terminal = await store.get_task(application_id="demo", job_id=handle.id)
        self.assertEqual(terminal.status, "failed")
        self.assertEqual(terminal.arguments, {})
        self.assertIsNone(terminal.invocation)
        with self.assertRaisesRegex(TaskExecutionError, "task_failed"):
            await handle.result()

    async def test_queued_task_can_create_owner_scoped_cron(self):
        async def schedule():
            """Use the public cron capability from inside a task attempt."""
            job = await cron.create(key="nested", expression="0 8 * * *", task=authored)
            return job.id

        authored, manager, store = await self.manager_for(schedule)
        active = _cron_invocation(SimpleNamespace(
            invocation_id="run-1", user_id="owner", session_id="session", metadata={},
        ))
        self.addCleanup(revoke_context, active)
        with activate_context(active):
            handle = await authored.defer()
        await self.execute_next(manager, store)
        with activate_context(active):
            schedule_id = await handle.result()
        stored = await store.get_cron(application_id="demo", schedule_id=schedule_id, user_id="owner")
        self.assertEqual(stored.user_id, "owner")


class TaskRuntimeConfigurationTests(unittest.TestCase):
    def test_queued_tasks_require_explicit_storage_even_with_database_url(self):
        """Environment variables must never select an implicit queue backend."""

        async def deliver():
            """Provide one compiler-discovered task."""

        _, _, application = _compiled(deliver)
        with patch.dict("os.environ", {"HARNEST_TASK_DATABASE_URL": "postgresql://unused"}):
            with self.assertRaisesRegex(TaskRuntimeError, "lifecycle.storage.tasks"):
                ProviderTaskRuntimeManager(application)


class TaskAuthoringTests(unittest.TestCase):
    def test_schedule_and_idempotency_controls_are_validated_before_runtime(self):
        @task
        def work(value):
            """Perform work."""

        async def exercise():
            with self.assertRaisesRegex(ValueError, "schedule_in"):
                await work.defer("x", schedule_in=float("nan"))
            with self.assertRaisesRegex(ValueError, "idempotency_key"):
                await work.defer("x", idempotency_key="")

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
