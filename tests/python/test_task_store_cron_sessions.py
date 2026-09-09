"""Cron occurrence sessions must exist before portable workers acquire their leases."""

from dataclasses import replace
import os
import time
import unittest
import uuid

from harnest import context
from harnest.cron_storage import CronRecord
from harnest.runtime_task_store import ProviderTaskRuntimeManager
from harnest.session import InMemorySessionStore
from harnest.task import MemoryTaskStore
from harnest.task_storage import TaskRecord
from test_task_store_runtime import application_for


async def _assert_cron_session(test, tasks, sessions, framework):
    """Reload a persisted occurrence and exercise the actual scoped session lease."""

    async def deliver(value):
        """Persist session data while observing only this occurrence's owner."""

        await context.session.set("delivered", value)
        return {"user": context.user_id, "framework": context.framework}

    _, application = application_for(deliver, tasks)
    application = replace(application, name=f"cron-session-{uuid.uuid4().hex}",
                          framework=framework, session_store=sessions)
    compiled = application.tasks[0]
    first = ProviderTaskRuntimeManager(application, manage_storage=False)
    schedule = CronRecord(uuid.uuid4().hex, application.name, "alice", "session-probe",
                          "* * * * *", compiled.name, {"value": "retained"}, time.time()-1)
    record = first._cron_task_record(compiled, schedule)
    session_id = record.invocation["session_id"]
    try:
        await tasks.enqueue_task(record)
        restarted = ProviderTaskRuntimeManager(application, manage_storage=False)
        claim = (await tasks.claim_tasks(application_id=application.name, queues=("default",),
                                         now=time.time(), lease_seconds=30))[0]
        await restarted._execute_record(claim)
        saved = await tasks.get_task(application_id=application.name, user_id="alice", job_id=record.job_id)
        test.assertEqual(saved.status, "completed")
        test.assertEqual(saved.result, {"user": "alice", "framework": framework})
        session = await sessions.get(session_id=session_id, user_id="alice")
        test.assertEqual(session.application_data, {"delivered": "retained"})
        test.assertIsNone(await sessions.get(session_id=session_id, user_id="bob"))
        # A retry/reclaimed attempt must retain state already committed by an
        # earlier attempt, not recreate or reset the occurrence's session.
        await restarted._ensure_cron_session(claim, claim.invocation)
        test.assertEqual((await sessions.get(session_id=session_id, user_id="alice")).application_data,
                         {"delivered": "retained"})
    finally:
        await sessions.delete(session_id=session_id, user_id="alice")


class CronSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_cron_creation_race_preserves_existing_session_and_ordinary_tasks(self):
        """Duplicate create keeps retry state, while ordinary tasks never recreate sessions."""

        class ConcurrentSessionStore(InMemorySessionStore):
            async def get(self, **scope):
                """Simulate a missing read just before another attempt creates the session."""

                return None

        async def deliver(value):
            """Provide a compiled target without executing a task in this race test."""

            return value

        sessions = ConcurrentSessionStore()
        _, application = application_for(deliver, MemoryTaskStore())
        manager = ProviderTaskRuntimeManager(replace(application, session_store=sessions))
        scope = {"session_id": "occurrence", "user_id": "alice"}
        await sessions.create(**scope, state={"retained": True})
        record = TaskRecord("job", application.name, "alice", application.tasks[0].name,
                            "default", {}, trigger="cron")
        await manager._ensure_cron_session(record, scope)
        self.assertEqual((await InMemorySessionStore.get(sessions, **scope)).state, {"retained": True})
        await manager._ensure_cron_session(replace(record, trigger="agent"), {
            "session_id": "deleted-agent-session", "user_id": "alice",
        })
        self.assertIsNone(await InMemorySessionStore.get(
            sessions, session_id="deleted-agent-session", user_id="alice"
        ))

    async def test_memory_workers_create_isolated_cron_sessions_for_both_frameworks(self):
        """Portable session stores cannot lease an occurrence that was never created."""

        for framework in ("adk", "langgraph"):
            with self.subTest(framework=framework):
                await _assert_cron_session(self, MemoryTaskStore(), InMemorySessionStore(), framework)

    @unittest.skipUnless(os.environ.get("HARNEST_TEST_POSTGRES_DSN"), "requires live PostgreSQL")
    async def test_postgres_cron_creates_session_before_real_lease(self):
        """Exercise PostgreSQL session persistence and leasing with exact cleanup."""

        from harnest_postgres import PostgresStore

        store = PostgresStore(os.environ["HARNEST_TEST_POSTGRES_DSN"])
        await store.start()
        try:
            for framework in ("adk", "langgraph"):
                await _assert_cron_session(self, MemoryTaskStore(), store, framework)
        finally:
            await store.close()

    @unittest.skipUnless(os.environ.get("HARNEST_TEST_REDIS_URL"), "requires live Redis")
    async def test_redis_cron_creates_session_before_real_lease(self):
        """Exercise Redis's distributed session lease with isolated keys."""

        from harnest_redis import RedisStore

        store = RedisStore(os.environ["HARNEST_TEST_REDIS_URL"], prefix="cron-session-" + uuid.uuid4().hex)
        await store.start()
        try:
            for framework in ("adk", "langgraph"):
                await _assert_cron_session(self, MemoryTaskStore(), store, framework)
        finally:
            await store.close()
