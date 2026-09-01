from __future__ import annotations

import asyncio
from dataclasses import replace
import os
import time
import unittest
from unittest.mock import patch
import uuid

from harnest.application import CompiledApplication
from harnest.cron import CompiledCron
from harnest.runtime_task import TaskRuntimeManager
from harnest.task import CompiledTask, registration_for, task


_POSTGRES_DSN = os.environ.get("HARNEST_TEST_POSTGRES_DSN")


@unittest.skipUnless(_POSTGRES_DSN, "requires HARNEST_TEST_POSTGRES_DSN")
class ProcrastinateTaskIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cron_dispatcher_commits_one_private_job_per_occurrence(self):
        """Exercise cron idempotency and opaque payloads against real PostgreSQL."""

        completed = asyncio.Event()

        @task(queue="integration")
        async def deliver(value):
            """Deliver one scheduled integration payload."""

            completed.set()
            return value

        definition = registration_for(deliver)
        assert definition is not None
        unique = uuid.uuid4().hex
        compiled = CompiledTask(
            name=f"harnest.integration_{unique}.tasks.deliver",
            source="tasks/deliver.py",
            definition=definition,
            authored=deliver,
        )
        schedule = CompiledCron(
            name=f"harnest.integration_{unique}.cron.daily",
            source="cron/daily.py",
            schedule="0 0 * * *",
            timezone="UTC",
            task=compiled,
            arguments={"value": "private-cron-value"},
        )
        application = CompiledApplication(
            name=f"integration_{unique}",
            framework="langgraph",
            mode="managed",
            target=object(),
            tasks=(compiled,),
            crons=(schedule,),
        )
        manager = TaskRuntimeManager(application)
        try:
            with patch.dict(
                os.environ, {"HARNEST_TASK_DATABASE_URL": _POSTGRES_DSN}
            ), self.assertLogs("procrastinate", level="INFO") as native_logs:
                await manager.start()
                await manager._enqueue_cron(schedule, 1_800_000_000)
                await manager._enqueue_cron(schedule, 1_800_000_000)
                await asyncio.wait_for(completed.wait(), timeout=10)
                jobs = tuple(
                    await manager._app.job_manager.list_jobs_async(
                        task=compiled.name
                    )
                )
                self.assertEqual(len(jobs), 1)
                payload_id = jobs[0].task_kwargs["_harnest_payload_id"]
                handle = manager._handle(
                    compiled, int(jobs[0].id), payload_id, "cron"
                )
                await _wait_for_status(handle, "succeeded")
            self.assertEqual(await handle.result(), "private-cron-value")
            self.assertNotIn("private-cron-value", " ".join(native_logs.output))
        finally:
            await manager.close()

    async def test_real_worker_schedules_retries_and_hides_payload_from_logs(self):
        """Exercise the native queue against Postgres without a model runtime."""

        attempts = []
        completed = asyncio.Event()

        @task(queue="integration", max_retries=2)
        async def deliver(value):
            """Deliver one integration-test payload."""

            attempts.append(value)
            if len(attempts) == 1:
                raise RuntimeError(value)
            completed.set()
            return {"delivered": value}

        definition = registration_for(deliver)
        assert definition is not None
        unique = uuid.uuid4().hex
        compiled = CompiledTask(
            name=f"harnest.integration_{unique}.tasks.deliver",
            source="tasks/deliver.py",
            definition=definition,
            authored=deliver,
        )
        application = CompiledApplication(
            name=f"integration_{unique}",
            framework="langgraph",
            mode="managed",
            target=object(),
            tasks=(compiled,),
        )
        manager = TaskRuntimeManager(application)
        started = time.monotonic()
        try:
            with patch.dict(
                os.environ, {"HARNEST_TASK_DATABASE_URL": _POSTGRES_DSN}
            ), self.assertLogs("procrastinate", level="INFO") as native_logs:
                await manager.start()
                handle = await deliver.defer("private-task-value", schedule_in=0.2)
                await asyncio.wait_for(completed.wait(), timeout=10)
                status = await _wait_for_status(handle, "succeeded")
            self.assertGreaterEqual(time.monotonic() - started, 0.15)
            self.assertEqual(attempts, ["private-task-value", "private-task-value"])
            self.assertEqual(status, "succeeded")
            self.assertEqual(
                await handle.result(), {"delivered": "private-task-value"}
            )
            self.assertNotIn("private-task-value", " ".join(native_logs.output))
            native_job = manager._app.job_manager
            jobs = tuple(await native_job.list_jobs_async(id=int(handle.id)))
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].attempts, 2)
        finally:
            await manager.close()


async def _wait_for_status(handle, expected):
    """Poll one native row until its worker-side transaction is visible."""

    for _attempt in range(100):
        status = await handle.status()
        if status == expected:
            return status
        await asyncio.sleep(0.05)
    return await handle.status()


if __name__ == "__main__":
    unittest.main()
