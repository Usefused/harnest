"""Run managed tools and workers through real PostgreSQL provider storage."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import os
import time
import unittest
from unittest.mock import patch
import uuid

from harnest import context, cron
from harnest.agent_principal import active_agent_principal
from harnest.context import activate_context, revoke_context
from harnest.runtime_task import TaskRuntimeDriver, TaskRuntimeError
from harnest.runtime_task_store import ProviderTaskRuntimeManager
from harnest.task_store_postgres import PostgresTaskStore

from test_task_runtime import _CronCallingDriver
from test_task_store_runtime import application_for, invocation, _cron_request


_DSN = os.environ.get("HARNEST_TEST_POSTGRES_DSN")


@unittest.skipUnless(_DSN, "set HARNEST_TEST_POSTGRES_DSN for provider runtime integration")
class PostgresProviderRuntimeTests(unittest.IsolatedAsyncioTestCase):
    """Verify released task/cron APIs at the worker and database boundary."""

    async def asyncSetUp(self):
        """Open a test-owned pool and isolate every application identity."""

        self.application_id = f"runtime-postgres-{uuid.uuid4().hex}"
        self.store = PostgresTaskStore(_DSN)
        await self.store.start()
        self.addAsyncCleanup(self.store.close)
        self.addAsyncCleanup(self._clear_scope)

    async def _clear_scope(self):
        """Delete only records owned by the generated integration application."""

        async with self.store._connection() as connection:
            await connection.execute(
                "DELETE FROM harnest_durable_tasks WHERE application_id=$1", self.application_id,
            )
            await connection.execute(
                "DELETE FROM harnest_durable_crons WHERE application_id=$1", self.application_id,
            )

    def application(self, function):
        """Reuse framework-independent compiled fixtures with a unique scope."""

        authored, application = application_for(function, self.store)
        return authored, replace(application, name=self.application_id)

    async def test_durable_task_executes_once_with_owner_and_terminal_scrub(self):
        """Managed defer restores identity, deduplicates, and commits a clean result."""

        observed = []

        async def deliver(value):
            """Observe restored runtime ownership inside the database worker."""

            observed.append((value, context.user_id))
            return {"delivered": True}

        authored, application = self.application(deliver)
        manager = ProviderTaskRuntimeManager(application, manage_storage=False)
        self.addAsyncCleanup(manager.close)
        owner, other = invocation(), invocation("other-user")
        self.addCleanup(revoke_context, owner)
        self.addCleanup(revoke_context, other)
        with patch("harnest.runtime_task._load_procrastinate", side_effect=AssertionError("legacy backend loaded")):
            await manager.start()
            with activate_context(owner):
                handle = await authored.defer(value="private-input", idempotency_key="once")
                retry = await authored.defer(value="private-input", idempotency_key="once")
            self.assertEqual(handle.id, retry.id)
            await _wait_for_success(handle)
        with activate_context(other):
            with self.assertRaises(TaskRuntimeError):
                await handle.result()
            with self.assertRaises(TaskRuntimeError):
                await handle.cancel()
        with activate_context(owner):
            self.assertEqual(await handle.result(), {"delivered": True})
        self.assertEqual(observed, [("private-input", "user-1")])
        persisted = await self.store.get_task(
            application_id=application.name, user_id="user-1", job_id=handle.id,
        )
        self.assertEqual(persisted.arguments, {})
        self.assertIsNone(persisted.invocation)
        self.assertIsNone(persisted.agent_permissions)

    async def test_tool_schedule_survives_restart_dispatches_owner_and_cancels(self):
        """A tool-created schedule survives worker replacement without widening grants."""

        observed = []
        completed = asyncio.Event()

        async def deliver(value):
            """Capture user and explicit empty scheduled-task permission grants."""

            principal = active_agent_principal()
            observed.append((value, context.user_id, principal.permissions))
            completed.set()
            return {"sent": True}

        authored, application = self.application(deliver)
        initial = ProviderTaskRuntimeManager(application, manage_storage=False)
        driver = TaskRuntimeDriver(_CronCallingDriver(authored), initial)
        self.addAsyncCleanup(driver.close)
        response = await driver.invoke(_cron_request())
        job = response.result
        self.assertEqual(job.status, "active")
        await driver.close()
        persisted = await self.store.get_cron(
            application_id=application.name, user_id="user-1", schedule_id=job.id,
        )
        await self.store.update_cron(
            replace(persisted, next_run_at=time.time()-1), expected_revision=persisted.revision,
        )
        restarted = ProviderTaskRuntimeManager(application, manage_storage=False)
        self.addAsyncCleanup(restarted.close)
        await restarted.start()
        await asyncio.wait_for(completed.wait(), timeout=5)
        owner, other = invocation(), invocation("other-user")
        self.addCleanup(revoke_context, owner)
        self.addCleanup(revoke_context, other)
        with activate_context(other), cron._activate_runtime(restarted.cron_runtime):
            self.assertIsNone(await cron.get(job.id))
            self.assertEqual(await cron.list(), ())
            with self.assertRaises(cron.CronNotFoundError):
                await cron.cancel(job.id)
        with activate_context(owner), cron._activate_runtime(restarted.cron_runtime):
            self.assertEqual((await cron.get(job.id)).status, "active")
            self.assertEqual((await cron.cancel(job.id)).status, "cancelled")
        await restarted.cron_runtime.dispatch(time.time()+8*24*60*60)
        self.assertEqual(observed, [("from-tool", "user-1", frozenset())])
        async with self.store._connection() as connection:
            count = await connection.fetchval(
                "SELECT count(*) FROM harnest_durable_tasks WHERE application_id=$1", application.name,
            )
        self.assertEqual(count, 1)


async def _wait_for_success(handle):
    """Wait for the durable commit rather than only the authored function return."""

    async def poll():
        """Bound polling to database state used by the released result API."""

        while await handle.status() != "succeeded":
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout=5)
