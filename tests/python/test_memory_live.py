"""Run custom-provider conformance against isolated persistent databases."""

import os
import unittest
import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch

from harnest.memory import MemoryConflictError, MemoryStorageError
from harnest_postgres import PostgresMemoryStore
from harnest_redis import RedisMemoryStore
from harnest.testing_memory import MemoryStoreConformanceMixin
from _memory_fault_proxy import LostReplyProxy
from dataclasses import replace
from harnest import context
from harnest.context import activate_context, revoke_context
from harnest.runtime_task_store import ProviderTaskRuntimeManager
from harnest_postgres import PostgresStore
from test_task_store_runtime import application_for, invocation


@unittest.skipUnless(os.environ.get("HARNEST_TEST_POSTGRES_DSN"), "requires PostgreSQL")
class PostgresMemoryTests(MemoryStoreConformanceMixin, unittest.IsolatedAsyncioTestCase):
    async def make_store(self):
        """Connect to the explicitly selected live test database."""
        return PostgresMemoryStore(os.environ["HARNEST_TEST_POSTGRES_DSN"])

    async def test_durable_task_restores_memory_owner(self):
        """Cross real task persistence into a worker with the same memory owner."""
        async def remember(value):
            """Deliberately write from the reconstructed durable task invocation."""
            saved = await context.memory.put('task-memory', value)
            return {'application': saved.scope.application_id, 'user': saved.scope.user_id}

        queue = PostgresStore(os.environ['HARNEST_TEST_POSTGRES_DSN'])
        authored, application = application_for(remember, queue)
        application = replace(application, name=self.scope.application_id, memory_store=self.store)
        manager = ProviderTaskRuntimeManager(application)
        active = invocation(owner=self.scope.user_id)
        self.addCleanup(revoke_context, active)
        try:
            await manager.start()
            with activate_context(active):
                handle = await authored.defer(value='saved by durable worker')
            for _ in range(250):
                if await handle.status() == 'succeeded':
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(await handle.result(), {'application': self.scope.application_id, 'user': self.scope.user_id})
            self.assertEqual((await self.store.get(self.scope, 'task-memory')).content, 'saved by durable worker')
        finally:
            await manager.close()

    async def test_fresh_provider_recovers_saved_memory(self):
        """Cross the connection lifecycle instead of reading an in-process cache."""
        record = await self.store.put(self.record())
        await self.store.close()
        restarted = await self.make_store()
        await restarted.start()
        self.addAsyncCleanup(restarted.close)
        self.assertEqual(await restarted.get(self.scope, record.key), record)

    async def test_connection_loss_before_commit_rolls_back_and_recovers(self):
        """Terminate the actual database backend after INSERT but before COMMIT."""
        self.addCleanup(self.store._pool.terminate)
        original = await self.store.put(self.record())
        inserted, release = asyncio.Event(), asyncio.Event()
        backend = []
        acquire = self.store._connection

        @asynccontextmanager
        async def paused_connection():
            """Pause transaction exit, not the real SQL mutation or database reply."""
            async with acquire() as connection:
                backend.append(connection.get_server_pid())
                async with connection.transaction():
                    yield connection
                    inserted.set()
                    await release.wait()

        with patch.object(self.store, '_connection', paused_connection):
            write = asyncio.create_task(self.store.put(self.record(content='replacement'), expected_revision=original.revision))
            try:
                await asyncio.wait_for(inserted.wait(), 5)
                async with acquire() as admin:
                    self.assertTrue(await admin.fetchval('SELECT pg_terminate_backend($1)', backend[0], timeout=5))
                release.set()
                with self.assertRaises(MemoryStorageError):
                    await asyncio.wait_for(write, 5)
            finally:
                release.set()
                await asyncio.gather(write, return_exceptions=True)
        self.assertEqual(await self.store.get(self.scope, original.key), original)
        self.assertEqual((await self.store.put(self.record(content='recovered'))).content, 'recovered')


@unittest.skipUnless(os.environ.get("HARNEST_TEST_REDIS_URL"), "requires Redis")
class RedisMemoryTests(MemoryStoreConformanceMixin, unittest.IsolatedAsyncioTestCase):
    async def test_user_erasure_spans_bounded_namespace_batches(self):
        """Cross the 100-namespace batch boundary without leaving indexed data."""
        for index in range(103):
            scope = replace(self.scope, namespace=f'collection-{index}')
            await self.store.put(self.record(scope=scope))
        self.assertEqual(await self.store.delete_user(self.scope.application_id, self.scope.user_id), 103)
        self.assertEqual(await self.store._eval(self.scope, "return redis.call('ZCARD',KEYS[3])"), 0)
        for index in range(103):
            scope = replace(self.scope, namespace=f'collection-{index}')
            self.assertEqual(await self.store._eval(scope, "return redis.call('EXISTS',KEYS[1],KEYS[2])"), 0)

    async def test_corrupt_namespace_membership_cannot_erase_foreign_data(self):
        """Treat the namespace index as untrusted when choosing erasure targets."""
        other = replace(self.scope, user_id='other-owner')
        record = await self.store.put(self.record(scope=other))
        foreign = await self.store._eval(other, 'return KEYS[1]')
        await self.store._eval(self.scope, "return redis.call('ZADD',KEYS[3],0,ARGV[2])", foreign)
        with self.assertRaises(MemoryStorageError):
            await self.store.delete_user(self.scope.application_id, self.scope.user_id)
        self.assertEqual(await self.store.get(other, record.key), record)
        await self.store._eval(self.scope, "return redis.call('ZREM',KEYS[3],ARGV[2])", foreign)

    async def make_store(self):
        """Keep memory keys separate from task/session test collections."""
        return RedisMemoryStore(os.environ["HARNEST_TEST_REDIS_URL"], prefix="explicit-memory-tests")

    async def test_sparse_search_has_bounded_scans_and_continuation(self):
        """An empty bounded search page must not hide later matches."""
        for index in range(130):
            await self.store.put(self.record(f"key-{index:03}", content="ordinary"))
        await self.store.put(self.record("z-last", content="needle"))
        first = await self.store.search(self.scope, "needle")
        self.assertEqual(first.items, ())
        self.assertIsNotNone(first.next_cursor)
        second = await self.store.search(self.scope, "needle", after=first.next_cursor)
        self.assertEqual([record.key for record in second.items], ["z-last"])

    async def test_fresh_provider_recovers_saved_memory(self):
        """Memory survives replacing the client without retaining Python state."""
        record = await self.store.put(self.record())
        await self.store.close()
        restarted = await self.make_store()
        await restarted.start()
        self.addAsyncCleanup(restarted.close)
        self.assertEqual(await restarted.get(self.scope, record.key), record)

    async def test_invalid_index_type_does_not_partially_write(self):
        """Lua errors must occur before any data mutation, not after HSET."""
        await self.store._eval(self.scope, "return redis.call('SET',KEYS[2],'wrong-type')")
        with self.assertRaises(MemoryStorageError):
            await self.store.put(self.record())
        self.assertEqual(await self.store._eval(self.scope, "return redis.call('HLEN',KEYS[1])"), 0)
        await self.store._eval(self.scope, "return redis.call('DEL',KEYS[2])")
        self.assertEqual((await self.store.put(self.record())).content, 'Concise reports')

    async def test_lost_write_reply_is_not_silently_replayed(self):
        """A committed write with a dropped TCP reply fails safely and is readable."""
        proxy = LostReplyProxy(os.environ['HARNEST_TEST_REDIS_URL'])
        url = await proxy.start()
        self.addAsyncCleanup(proxy.close)
        store = RedisMemoryStore(url, prefix='memory-fault-tests')
        await store.start()
        self.addAsyncCleanup(store.close)
        proxy.drop_reply = True
        with self.assertRaises(MemoryStorageError):
            await asyncio.wait_for(store.put(self.record()), 5)
        saved = await store.get(self.scope, 'preference')
        self.assertIsNotNone(saved)
        self.assertEqual(saved.content, 'Concise reports')
        newer = await store.put(self.record(content='new revision'), expected_revision=saved.revision)
        with self.assertRaises(MemoryConflictError):
            await store.put(self.record(), expected_revision=saved.revision)
        self.assertEqual(await store.get(self.scope, newer.key), newer)
