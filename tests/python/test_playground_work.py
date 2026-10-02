"""Live work authorization, privacy, concurrency, and datastore query boundaries."""

from contextlib import asynccontextmanager
from dataclasses import replace
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import httpx

from harnest.application import CompiledApplication
from harnest._cron_storage import cron_fingerprint
from harnest.cron import cron, CronRecord
from harnest.neutral_runtime import create_neutral_app
from harnest.runtime_auth import AuthPrincipal, AuthenticationError
from harnest.runtime_task import TaskRuntimeDriver
from harnest.runtime_task_store import ProviderTaskRuntimeManager
from harnest.task import CompiledTask, registration_for
from harnest.task import TaskRecord
from harnest.task_store_memory import MemoryTaskStore
from harnest.task_store_postgres import PostgresTaskStore
from harnest.task_store_redis import RedisTaskStore, _task_dump
from test_neutral_runtime import FakeDriver


class WorkAuthenticator:
    """Test-only identity provider; production claims come from the deployed authenticator."""
    async def authenticate(self, connection):
        owner = connection.headers.get("x-test-user")
        if not owner:
            raise AuthenticationError()
        return AuthPrincipal(owner, {"harnest:work:automation": owner == "admin"})


def job(identity, owner="alice", **options):
    """Keep workers idle while testing private payload exclusion and mutations."""
    return TaskRecord(job_id=identity, application_id="work_test", user_id=owner,
        task_name="harnest.work_test.tasks.cron.deliver", queue="default",
        arguments={"payload": "private"}, scheduled_at=time.time() + 86400, **options)


class PlaygroundWorkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        """Start the real task manager and neutral HTTP stack over memory persistence."""
        @cron()
        async def deliver(payload=""):
            """Provide a compiled target without contacting a model."""
            return payload
        task = CompiledTask(name="harnest.work_test.tasks.cron.deliver", source="cron/deliver.py", definition=registration_for(deliver), authored=deliver)
        self.store = MemoryTaskStore()
        app = CompiledApplication(name="work_test", framework="langgraph", mode="managed", target=object(), tasks=(task,), task_store=self.store, cron_store=self.store)
        self.manager = ProviderTaskRuntimeManager(app)
        self.driver = TaskRuntimeDriver(FakeDriver(), self.manager)
        self.app = create_neutral_app(self.driver, authenticator=WorkAuthenticator(), playground_enabled=True)
        await self.enterAsyncContext(self.app.router.lifespan_context(self.app))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://runtime", headers={"x-test-user": "alice"})
        self.addAsyncCleanup(self.client.aclose)

    async def test_task_owner_scope_projection_cancellation_and_origin(self):
        """Never disclose foreign work or payloads; only authenticated same-origin mutations commit."""
        for record in (job("a"), job("b"), job("foreign", "bob"), job("auto", "_harnest_automation")):
            await self.store.enqueue_task(record)
        base = "/_harnest/work/tasks"
        page = (await self.client.get(base + "?limit=1")).json()
        self.assertEqual([item["job_id"] for item in page["items"]], ["a"])
        self.assertNotIn("private", json.dumps(page))
        self.assertNotIn("arguments", page["items"][0])
        self.assertEqual((await self.client.get(base + "?after=a")).json()["items"][0]["job_id"], "b")
        self.assertEqual((await self.client.get(base + "/foreign")).status_code, 404)
        self.assertEqual((await self.client.post(base + "/foreign/cancel")).status_code, 404)
        self.assertEqual((await self.client.get(base + "?scope=automation")).status_code, 403)
        self.assertEqual((await self.client.get(base + "?scope=everyone")).status_code, 422)
        self.assertEqual((await self.client.post(base + "/a/cancel", headers={"Origin": "https://foreign.example"})).status_code, 403)
        with patch("harnest.runtime_task_store._AUDIT") as logger:
            audit = logger.info
            self.assertTrue((await self.client.post(base + "/a/cancel")).json()["cancelled"])
            self.assertEqual(audit.call_args.kwargs["trigger"], "user")
            self.assertEqual(audit.call_args.kwargs["outcome"], "committed")
        self.assertEqual((await self.client.get(base + "/a")).json()["status"], "cancelled")
        self.assertEqual((await self.client.get(base + "?scope=automation", headers={"x-test-user":"admin"})).json()["items"][0]["job_id"], "auto")
        self.assertEqual((await self.client.get(base, headers={"x-test-user":""})).status_code, 401)
        self.assertEqual((await self.client.get(base + "?limit=101")).status_code, 422)

    async def test_cron_crud_uses_deployed_targets_revisions_and_terminal_policy(self):
        """Exercise the complete schedule lifecycle with cross-owner and stale-edit rejection."""
        base = "/_harnest/work/crons"
        body = {"key":"daily", "task":"deliver", "expression":"0 9 * * *", "arguments":{"payload":"private"}}
        self.assertEqual((await self.client.post(base, json={**body, "task":"undeployed"})).status_code, 422)
        self.assertEqual((await self.client.post(base, json={**body, "user_id":"bob"})).status_code, 422)
        with patch("harnest.runtime_cron_store._AUDIT") as logger:
            audit = logger.info
            response = await self.client.post(base, json=body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(audit.call_args.kwargs["trigger"], "user")
        identity = response.json()["id"]
        url = f"{base}/{identity}"
        self.assertNotIn("private", (await self.client.get(base)).text)
        self.assertEqual((await self.client.get(url, headers={"x-test-user":"bob"})).status_code, 404)
        self.assertEqual((await self.client.delete(url, headers={"x-test-user":"bob"})).status_code, 404)
        record = (await self.client.get(url)).json()
        edit = {"expression":"0 10 * * *", "revision":record["revision"]}
        self.assertEqual((await self.client.patch(url, json=edit)).status_code, 200)
        self.assertEqual((await self.client.patch(url, json=edit)).status_code, 409)
        self.assertEqual((await self.client.get(url)).json()["arguments"], body["arguments"])
        for status in ("paused", "active", "cancelled"):
            self.assertEqual((await self.client.post(url + "/status", json={"status":status})).status_code, 200)
        self.assertEqual((await self.client.post(url + "/status", json={"status":"active"})).status_code, 409)
        self.assertTrue((await self.client.delete(url)).json()["deleted"])
        self.assertEqual((await self.client.get(url)).status_code, 404)

    async def test_fixed_schedule_protection_provider_errors_and_disabled_playground(self):
        """Fixed schedules stay source-owned and errors never echo provider diagnostics."""
        await self.store.create_cron(CronRecord(schedule_id="fixed", application_id="work_test", user_id="_harnest_automation", key="static:fixed", expression="0 9 * * *", task_name="harnest.work_test.tasks.cron.deliver", arguments={}, next_run_at=time.time()+86400))
        response = await self.client.post("/_harnest/work/crons/fixed/status?scope=automation", json={"status":"paused"}, headers={"x-test-user":"admin"})
        self.assertEqual(response.status_code, 409)
        with patch.object(self.store, "list_task_metadata", AsyncMock(side_effect=RuntimeError("private-dsn"))):
            response = await self.client.get("/_harnest/work/tasks")
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("private-dsn", response.text)
        with patch.object(self.store, "list_task_metadata", None):
            self.assertEqual((await self.client.get("/_harnest/work/tasks")).status_code, 501)
            self.assertFalse((await self.client.get("/_harnest/work")).json()["task_listing"])
        app = create_neutral_app(FakeDriver(), playground_enabled=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://runtime") as client:
            self.assertEqual((await client.get("/_harnest/work")).status_code, 404)


class WorkStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_redis_legacy_schedule_retry_keeps_its_identity(self):
        """New optional limits cannot break idempotency of persisted schedules."""

        client = fakeredis.aioredis.FakeRedis()
        self.addAsyncCleanup(client.aclose)
        store = RedisTaskStore("redis://unused", _client=client)
        record = CronRecord(schedule_id="old", application_id="work_test", user_id="alice",
                            key="old", expression="* * * * *", task_name=job("a").task_name,
                            arguments=job("a").arguments, next_run_at=10)
        await store.create_cron(record)
        cron_key = store._durable_key("work_test", "crons")
        raw = json.loads(await client.hget(cron_key, "old"))
        for name in ("max_runs", "max_consecutive_failures", "run_count", "consecutive_failures"):
            raw.pop(name)
        raw["_fingerprint"] = cron_fingerprint(record, legacy=True)
        await client.hset(cron_key, "old", json.dumps(raw))
        replay = await store.create_cron(replace(record, schedule_id="new"))
        self.assertEqual((replay.schedule_id, replay.run_count), ("old", 0))

    async def test_redis_cron_limits_survive_terminal_task_outcomes(self):
        """Run caps and the resettable failure streak execute in one Lua state."""

        client = fakeredis.aioredis.FakeRedis()
        self.addAsyncCleanup(client.aclose)
        store = RedisTaskStore("redis://unused", _client=client)
        await store.create_cron(CronRecord(
            schedule_id="limited", application_id="work_test", user_id="alice",
            key="limited", expression="* * * * *", task_name=job("a").task_name,
            arguments=job("a").arguments, next_run_at=10,
            max_runs=4, max_consecutive_failures=2,
        ))
        for index, outcome in enumerate(("failed", "completed", "failed", "failed")):
            current = await store.get_cron(application_id="work_test", user_id="alice",
                                           schedule_id="limited")
            due = current.next_run_at
            task = replace(job(f"limited-{index}"), scheduled_at=due,
                           cron_schedule_id="limited", max_retries=0)
            await store.commit_cron_occurrence(
                application_id="work_test", user_id="alice", schedule_id="limited",
                expected_revision=current.revision, due_at=due, next_run_at=due + 60,
                task=task,
            )
            claimed, = await store.claim_tasks(application_id="work_test", queues=("default",),
                                               now=due, lease_seconds=5)
            await store.finish_task(application_id="work_test", job_id=claimed.job_id,
                                    lease_token=claimed.lease_token, now=due + 1, status=outcome)
        current = await store.get_cron(application_id="work_test", user_id="alice",
                                       schedule_id="limited")
        self.assertEqual((current.run_count, current.consecutive_failures, current.status),
                         (4, 2, "cancelled"))

    async def test_redis_lua_backfills_legacy_jobs_and_indexes_new_occurrences(self):
        """Execute actual Lua over legacy and new records, checking page scope and privacy."""
        client = fakeredis.aioredis.FakeRedis()
        self.addAsyncCleanup(client.aclose)
        store = RedisTaskStore("redis://unused", _client=client)
        legacy = job("a")
        await client.hset(store._durable_key("work_test", "jobs"), "a", _task_dump(legacy))
        await store.enqueue_task(job("b"))
        await store.enqueue_task(job("foreign", "bob"))
        page = await store.list_task_metadata(application_id="work_test", user_id="alice", limit=1)
        while page["indexing"]:
            page = await store.list_task_metadata(application_id="work_test", user_id="alice", limit=1)
        self.assertEqual(page["items"][0]["job_id"], "a")
        self.assertNotIn("private", json.dumps(page))
        self.assertEqual((await store.list_task_metadata(application_id="work_test", user_id="alice", after="a"))["items"][0]["job_id"], "b")
        schedule = CronRecord(schedule_id="schedule", application_id="work_test", user_id="alice", key="scheduled", expression="* * * * *", task_name=legacy.task_name, arguments=legacy.arguments, next_run_at=10)
        await store.create_cron(schedule)
        await store.commit_cron_occurrence(application_id="work_test", user_id="alice", schedule_id="schedule", expected_revision=0, due_at=10, next_run_at=70, task=replace(job("c"), scheduled_at=10, cron_schedule_id="schedule"))
        self.assertEqual([row["job_id"] for row in (await store.list_task_metadata(application_id="work_test", user_id="alice", after="b"))["items"]], ["c"])
        page = await store.list_cron_metadata(application_id="work_test", user_id="alice", limit=1)
        self.assertEqual(page["items"][0]["schedule_id"], "schedule")
        self.assertNotIn("arguments", page["items"][0])
        self.assertEqual((await store.list_cron_metadata(application_id="work_test", user_id="bob"))["items"], [])

    async def test_postgres_listing_is_one_projected_owner_query_per_page(self):
        """Assert query count and pushed-down constraints without a SQL interpreter."""
        store = PostgresTaskStore("unused")
        connection = AsyncMock()
        connection.fetch.return_value = []
        @asynccontextmanager
        async def connected():
            """Supply a recording connection at the real provider query boundary."""
            yield connection
        with patch.object(store, "_connection", connected):
            for listing in (store.list_task_metadata, store.list_cron_metadata):
                connection.fetch.reset_mock()
                await listing(application_id="app", user_id="alice", after="cursor", limit=3)
                connection.fetch.assert_awaited_once()
                query, *arguments = connection.fetch.call_args.args
                self.assertEqual(arguments, ["app", "alice", "cursor", 3])
                self.assertIn("user_id=$2", query)
                self.assertIn("ORDER BY", query)
                self.assertIn("LIMIT $4", query)
                self.assertNotIn("SELECT *", query)
                self.assertNotIn("arguments", query)
