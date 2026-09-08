"""Redis serialization, indexed-query, and live durable-provider coverage."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import os
import time
import unittest
from unittest.mock import AsyncMock
import uuid

from harnest.cron_storage import CronRecord, CronStoreConflictError
from harnest.task_storage import TaskRecord, TaskStoreConflictError
from harnest.task_store_redis import RedisTaskStore, _cron_dump, _due_member, _task_dump, _task_load
from harnest.testing import TaskStoreConformanceMixin


def _job(job_id="job", **options):
    """Construct a deterministic record with overrides for contract scenarios."""

    values = dict(job_id=job_id, application_id="app", user_id="owner", task_name="tasks.work",
                  queue="default", arguments={"value": []}, scheduled_at=10, max_retries=1,
                  invocation={"user_id": "owner"}, agent_permissions=("read",))
    values.update(options)
    return TaskRecord(**values)


def _cron(schedule_id="cron", **options):
    """Construct one owner-scoped schedule with predictable due ordering."""

    values = dict(schedule_id=schedule_id, application_id="app", user_id="owner", key=schedule_id,
                  expression="* * * * *", task_name="tasks.work", arguments={"value": []}, next_run_at=10)
    values.update(options)
    return CronRecord(**values)


class RedisTaskStoreUnitTests(unittest.IsolatedAsyncioTestCase):
    """Check datastore query boundaries without requiring a Redis process."""

    def test_opaque_payload_roundtrip_and_no_eager_redis_import(self):
        record = _job(arguments={"empty_list": [], "empty_object": {}, "large": 2**60}, result=[{}, []])
        self.assertEqual(_task_load(_task_dump(record)), record)

    def test_app_keys_share_cluster_slot_and_isolate_app_and_prefix(self):
        store = RedisTaskStore("redis://unused", prefix="one")
        keys = (*store._enqueue_keys(_job()), *store._cron_keys("app", "owner"))
        slots = {key.split("{")[1].split("}")[0] for key in keys}
        self.assertEqual(len(slots), 1)
        self.assertNotEqual(store._durable_key("app", "jobs"), store._durable_key("other", "jobs"))
        other = RedisTaskStore("redis://unused", prefix="two")
        self.assertNotEqual(store._durable_key("app", "jobs"), other._durable_key("app", "jobs"))

    async def test_owner_and_due_pages_use_one_bounded_redis_query(self):
        client = AsyncMock()
        client.eval.return_value = [_cron_dump(_cron())]
        store = RedisTaskStore("redis://unused", _client=client)
        self.assertEqual(len(await store.list_crons(application_id="app", user_id="owner", limit=3)), 1)
        client.eval.assert_awaited_once()
        self.assertEqual(client.eval.call_args.args[-2:], (3, "owner"))
        client.eval.reset_mock()
        await store.list_due_crons(application_id="app", now=20, after=(10, "cron"), limit=2)
        client.eval.assert_awaited_once()
        self.assertEqual(client.eval.call_args.args[-4], "(" + _due_member(10, "cron"))
        self.assertGreater(client.eval.call_args.args[-3][1:], _due_member(20, "😀"))
        self.assertEqual(client.eval.call_args.args[-2:], (2, "due"))

    def test_due_cursor_preserves_float_order_without_rounding(self):
        values = [0, 0.000000001, 1, 10, 10.00000000001, 1_800_000_000]
        self.assertEqual(sorted(_due_member(value, "id") for value in values),
                         [_due_member(value, "id") for value in values])


class _RecordingClient:
    """Track only test-created keys so teardown never scans shared Redis data."""

    def __init__(self, client):
        self.client = client
        self.keys = set()

    async def eval(self, script, count, *arguments):
        """Record the explicit transaction keys and execute the real Lua script."""

        self.keys.update(arguments[:count])
        return await self.client.eval(script, count, *arguments)

    async def set(self, key, *arguments, **options):
        """Track the provider schema marker created during startup."""

        self.keys.add(key)
        return await self.client.set(key, *arguments, **options)

    async def get(self, key):
        return await self.client.get(key)


@unittest.skipUnless(os.environ.get("HARNEST_TEST_REDIS_URL"), "set HARNEST_TEST_REDIS_URL for live Redis tasks")
class LiveRedisTaskStoreTests(unittest.IsolatedAsyncioTestCase):
    """Exercise real atomic scripts with concurrent workers and scoped records."""

    async def asyncSetUp(self):
        import redis.asyncio
        self.client = redis.asyncio.from_url(os.environ["HARNEST_TEST_REDIS_URL"])
        self.recording = _RecordingClient(self.client)
        self.store = RedisTaskStore("redis://injected", prefix="harnest-test-" + uuid.uuid4().hex,
                                    _client=self.recording)
        await self.store.start()

    async def asyncTearDown(self):
        if self.recording.keys:
            await self.client.delete(*self.recording.keys)
        await self.store.close()
        await self.client.aclose()

    async def test_concurrent_enqueue_claim_and_fenced_completion(self):
        record = _job(idempotency_key="once", arguments={"array": [], "object": {}, "large": 2**60})
        submissions = await asyncio.gather(*(self.store.enqueue_task(replace(record, job_id=str(i))) for i in range(8)))
        self.assertEqual(len({item.job_id for item in submissions}), 1)
        claims = await asyncio.gather(*(self.store.claim_tasks(application_id="app", queues=("default",),
                                                              now=10, lease_seconds=5) for _ in range(8)))
        claim = next(page[0] for page in claims if page)
        self.assertEqual(sum(len(page) for page in claims), 1)
        renewed = await self.store.renew_task_lease(application_id="app", job_id=claim.job_id,
                                                   lease_token=claim.lease_token, now=14, lease_seconds=5)
        self.assertTrue(renewed)
        self.assertEqual(await self.store.claim_tasks(application_id="app", queues=("default",), now=16, lease_seconds=5), ())
        reclaimed = (await self.store.claim_tasks(application_id="app", queues=("default",), now=20, lease_seconds=5))[0]
        self.assertEqual(reclaimed.attempt, 2)
        self.assertFalse(await self.store.finish_task(application_id="app", job_id=claim.job_id,
                                                      lease_token=claim.lease_token, now=20, status="completed"))
        result = {"array": [], "object": {}, "large": 2**60}
        self.assertTrue(await self.store.finish_task(application_id="app", job_id=claim.job_id,
                                                     lease_token=reclaimed.lease_token, now=21, status="completed", result=result))
        saved = await self.store.enqueue_task(record)
        self.assertEqual(saved.result, result)
        self.assertEqual(saved.arguments, {})
        self.assertIsNone(saved.invocation)
        self.assertIsNone(saved.agent_permissions)
        with self.assertRaises(TaskStoreConflictError):
            await self.store.enqueue_task(replace(record, arguments={"different": True}))

    async def test_claim_order_retry_budget_cancellation_and_scope(self):
        await self.store.enqueue_task(_job("later", queue="alpha", scheduled_at=11))
        await self.store.enqueue_task(_job("earlier", queue="beta", max_retries=0))
        claim = (await self.store.claim_tasks(application_id="app", queues=("alpha", "beta"), now=12, lease_seconds=1))[0]
        self.assertEqual(claim.job_id, "earlier")
        next_claim = (await self.store.claim_tasks(application_id="app", queues=("alpha", "beta"), now=14, lease_seconds=1))[0]
        self.assertEqual(next_claim.job_id, "later")
        failed = await self.store.get_task(application_id="app", job_id="earlier")
        self.assertEqual((failed.status, failed.failure_code, failed.arguments), ("failed", "task_failed", {}))
        self.assertIsNone(await self.store.get_task(application_id="app", user_id="other", job_id="later"))
        self.assertIsNone(await self.store.get_task(application_id="app", user_id="", job_id="later"))
        self.assertFalse(await self.store.cancel_task(application_id="app", user_id="other", job_id="later", now=14))
        self.assertTrue(await self.store.cancel_task(application_id="app", user_id="owner", job_id="later", now=14))
        self.assertFalse(await self.store.finish_task(application_id="app", job_id="later", lease_token=next_claim.lease_token,
                                                      now=14, status="completed"))

    async def test_cron_pages_and_atomic_occurrence_with_stale_races(self):
        for record in (_cron("a"), _cron("b"), _cron("c", next_run_at=20), _cron("foreign", user_id="other")):
            await self.store.create_cron(record)
        self.assertIsNone(await self.store.get_cron(application_id="app", user_id="", schedule_id="a"))
        self.assertEqual([r.schedule_id for r in await self.store.list_crons(application_id="app", user_id="owner", limit=2)], ["a", "b"])
        self.assertEqual([r.schedule_id for r in await self.store.list_due_crons(application_id="app", now=10, after=(10, "a"), limit=1)], ["b"])
        outcomes = await asyncio.gather(*(self.store.commit_cron_occurrence(
            application_id="app", user_id="owner", schedule_id="a", expected_revision=0,
            due_at=10, next_run_at=70, task=_job("occurrence-" + str(i), idempotency_key="a:10")
        ) for i in range(8)))
        self.assertEqual(sum(item is not None for item in outcomes), 1)
        after = await self.store.get_cron(application_id="app", user_id="owner", schedule_id="a")
        self.assertEqual((after.revision, after.next_run_at), (1, 70))
        cancelled = await self.store.update_cron(replace(after, status="cancelled"), expected_revision=1)
        self.assertEqual(await self.store.create_cron(_cron("different", key="a")), cancelled)
        with self.assertRaises(CronStoreConflictError):
            await self.store.update_cron(replace(cancelled, status="active"), expected_revision=2)
        self.assertTrue(await self.store.delete_cron(application_id="app", user_id="owner", schedule_id="b"))
        page = await self.store.list_due_crons(application_id="app", now=20, after=(10, "b"), limit=10)
        self.assertEqual([r.schedule_id for r in page], ["foreign", "c"])

    async def test_managed_runtime_executes_tasks_and_recovers_owner_cron_after_restart(self):
        """Run authored code and restored schedules through real Redis storage."""

        from harnest import context, cron
        from harnest.context import activate_context, revoke_context
        from harnest.runtime_task_store import ProviderTaskRuntimeManager
        from test_task_store_runtime import application_for, invocation

        observed = []
        delivered = asyncio.Event()

        async def deliver(value):
            """Capture restored identity without a model provider or network call."""
            observed.append((value, context.user_id))
            if value == "scheduled":
                delivered.set()
            return {"value": value}

        authored, application = application_for(deliver, self.store)
        first = ProviderTaskRuntimeManager(application, manage_storage=False)
        active, foreign = invocation(), invocation("other")
        second = None
        try:
            await first.start()
            with activate_context(active), cron._activate_runtime(first.cron_runtime):
                handle = await authored.defer(value="direct", idempotency_key="direct")
                schedule = await cron.create(key="live", expression="* * * * *", task=authored,
                                             arguments={"value": "scheduled"})
            await _wait_for_status(handle, "succeeded")
            self.assertEqual(await handle.result(), {"value": "direct"})
            await first.close()
            # A new provider/client facade proves no in-process schedule cache
            # supplies the restarted runtime's occurrence or owner identity.
            fresh = RedisTaskStore("redis://injected", prefix=self.store._prefix, _client=self.recording)
            await fresh.start()
            self.addAsyncCleanup(fresh.close)
            record = await fresh.get_cron(application_id=application.name, user_id="user-1", schedule_id=schedule.id)
            await fresh.update_cron(replace(record, next_run_at=time.time()-1), expected_revision=record.revision)
            restarted = replace(application, task_store=fresh, cron_store=fresh)
            second = ProviderTaskRuntimeManager(restarted, manage_storage=False)
            await second.start()
            await asyncio.wait_for(delivered.wait(), 3)
            with activate_context(foreign), cron._activate_runtime(second.cron_runtime):
                self.assertIsNone(await cron.get(schedule.id))
                with self.assertRaises(cron.CronNotFoundError):
                    await cron.cancel(schedule.id)
            with activate_context(active), cron._activate_runtime(second.cron_runtime):
                await cron.cancel(schedule.id)
            await second.cron_runtime.dispatch(time.time()+120)
            self.assertEqual(observed, [("direct", "user-1"), ("scheduled", "user-1")])
        finally:
            revoke_context(active)
            revoke_context(foreign)
            await first.close()
            if second is not None:
                await second.close()


async def _wait_for_status(handle, status):
    """Wait for the committed worker result rather than only authored execution."""

    for _ in range(200):
        if await handle.status() == status:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("task did not reach the expected committed status")


@unittest.skipUnless(os.environ.get("HARNEST_TEST_REDIS_URL"), "set HARNEST_TEST_REDIS_URL for live Redis conformance")
class LiveRedisTaskStoreConformance(TaskStoreConformanceMixin, unittest.IsolatedAsyncioTestCase):
    """Run the identical public-provider contract against real Redis scripts."""

    async def make_store(self):
        """Isolate provider keys and register exact-key cleanup before startup."""

        import redis.asyncio
        self.client = redis.asyncio.from_url(os.environ["HARNEST_TEST_REDIS_URL"])
        self.recording = _RecordingClient(self.client)
        self.addAsyncCleanup(self.client.aclose)
        self.addAsyncCleanup(self._remove_test_keys)
        return RedisTaskStore("redis://injected", prefix="harnest-test-" + uuid.uuid4().hex,
                              _client=self.recording)

    async def _remove_test_keys(self):
        """Delete only keys explicitly supplied by this isolated provider."""

        if self.recording.keys:
            await self.client.delete(*self.recording.keys)


if __name__ == "__main__":
    unittest.main()
