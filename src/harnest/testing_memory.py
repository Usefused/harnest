"""Reusable database conformance tests for custom explicit-memory providers."""

import asyncio
from dataclasses import replace
import time
from typing import Any
import uuid

from .memory import MemoryConflictError, MemoryRecord, MemoryScope, MemoryStore


class MemoryStoreConformanceMixin:
    """Combine with IsolatedAsyncioTestCase and implement async make_store()."""

    async def make_store(self) -> MemoryStore:
        """Return an unstarted provider; register any test-data cleanup yourself."""
        raise NotImplementedError

    async def asyncSetUp(self) -> None:
        """Own a fresh provider and unique application scope for each check."""
        self.scope = MemoryScope("memory-test-" + uuid.uuid4().hex, "alice")
        self.store = await self.make_store()
        await self.store.start()
        self.addAsyncCleanup(self.store.close)

    def record(self, key: str = "preference", **changes: Any) -> MemoryRecord:
        """Build a valid explicit write without coupling tests to a datastore."""
        return replace(MemoryRecord(self.scope, key, "Concise reports", {"labels": ["style"]}), **changes)

    async def test_memory_is_scoped_and_explicit(self) -> None:
        """Identical keys never cross application, user or namespace boundaries."""
        self.assertIsNone(await self.store.get(self.scope, "preference"))
        stored = await self.store.put(self.record())
        self.assertEqual(await self.store.get(self.scope, stored.key), stored)
        scopes = [replace(self.scope, user_id="bob"), replace(self.scope, application_id="other"), replace(self.scope, namespace="other")]
        for scope in scopes:
            self.assertIsNone(await self.store.get(scope, stored.key))
            self.assertEqual((await self.store.search(scope, "reports")).items, ())
            self.assertFalse(await self.store.delete(scope, stored.key))

    async def test_memory_results_are_detached(self) -> None:
        """Callers cannot silently mutate persisted metadata through returned objects."""
        record = await self.store.put(self.record())
        record.metadata["labels"].append("changed")
        self.assertEqual((await self.store.get(self.scope, record.key)).metadata["labels"], ["style"])

    async def test_memory_json_roundtrip_is_lossless(self) -> None:
        """Provider scripting must preserve arrays, exact integers and floats."""
        metadata = {"empty": [], "object": {}, "nested": [[], {}, [None]],
                    "large": 2**60 + 1, "negative": -(2**60 + 1), "float": 1.2345678901234567,
                    "unicode": "你好 🧠", "boolean": False}
        original = await self.store.put(self.record(metadata=metadata))
        self.assertEqual(original.metadata, metadata)
        updated = await self.store.put(self.record(metadata=metadata), expected_revision=original.revision)
        self.assertEqual(updated.metadata, metadata)
        self.assertEqual(updated.created_at, original.created_at)
        self.assertEqual(await self.store.get(self.scope, updated.key), updated)
        self.assertEqual((await self.store.list(self.scope)).items, (updated,))

    async def test_memory_maintenance_preserves_other_owners(self) -> None:
        """Bulk erasure includes expired records but never another application/user."""
        namespace = replace(self.scope, namespace='private')
        others = [replace(self.scope, user_id='bob'), replace(self.scope, application_id=self.scope.application_id + '-other')]
        await self.store.put(self.record(expires_at=time.time() + 0.1))
        await self.store.put(self.record(scope=namespace))
        for scope in others:
            await self.store.put(self.record(scope=scope))
        await asyncio.sleep(0.15)
        self.assertEqual(await self.store.delete_all(self.scope), 1)
        self.assertIsNotNone(await self.store.get(namespace, 'preference'))
        self.assertEqual(await self.store.delete_user(self.scope.application_id, self.scope.user_id), 1)
        self.assertEqual(await self.store.delete_user(self.scope.application_id, self.scope.user_id), 0)
        self.assertIsNone(await self.store.get(namespace, 'preference'))
        for scope in others:
            self.assertIsNotNone(await self.store.get(scope, 'preference'))

    async def test_memory_purge_physically_removes_expired_pages(self) -> None:
        """Zero-deletion pages continue and live data survives a bounded sweep."""
        await self.store.put(self.record('a-live'))
        await self.store.put(self.record('b-expired', expires_at=time.time() + 0.1))
        await self.store.put(self.record('c-live'))
        await asyncio.sleep(0.15)
        first = await self.store.purge_expired(self.scope, limit=1)
        self.assertEqual(first.deleted, 0)
        self.assertIsNotNone(first.next_cursor)
        second = await self.store.purge_expired(self.scope, limit=1, after=first.next_cursor)
        self.assertEqual(second.deleted, 1)
        self.assertIsNotNone(second.next_cursor)
        last = await self.store.purge_expired(self.scope, limit=1, after=second.next_cursor)
        self.assertEqual(last.deleted, 0)
        self.assertIsNone(last.next_cursor)
        self.assertEqual(await self.store.delete_all(self.scope), 2)

    async def test_memory_revision_fences_concurrent_writers(self) -> None:
        """Exactly one writer can replace the same revision across concurrent calls."""
        original = await self.store.put(self.record())
        results = await asyncio.gather(*(
            self.store.put(self.record(content=f"revision {number}"), expected_revision=original.revision)
            for number in range(5)
        ), return_exceptions=True)
        winners = [result for result in results if isinstance(result, MemoryRecord)]
        self.assertEqual(len(winners), 1)
        self.assertEqual(sum(isinstance(result, MemoryConflictError) for result in results), 4)
        self.assertEqual(winners[0].created_at, original.created_at)
        self.assertNotEqual(winners[0].revision, original.revision)

    async def test_memory_delete_does_not_allow_stale_resurrection(self) -> None:
        """Deletion removes search membership and invalidates earlier revisions."""
        record = await self.store.put(self.record())
        with self.assertRaises(MemoryConflictError):
            await self.store.delete(self.scope, record.key, expected_revision="stale")
        self.assertTrue(await self.store.delete(self.scope, record.key, expected_revision=record.revision))
        self.assertEqual((await self.store.search(self.scope, "reports")).items, ())
        with self.assertRaises(MemoryConflictError):
            await self.store.put(self.record(), expected_revision=record.revision)
        replacement = await self.store.put(self.record())
        self.assertNotEqual(record.revision, replacement.revision)

    async def test_memory_pages_and_literal_search(self) -> None:
        """Paginate datastore-filtered results without interpreting wildcard syntax."""
        for key in ("a", "b", "c", "d"):
            await self.store.put(self.record(key, content="literal %_[] match"))
        first = await self.store.search(self.scope, "%_[]", limit=2)
        self.assertEqual([record.key for record in first.items], ["a", "b"])
        second = await self.store.search(self.scope, "%_[]", limit=2, after=first.next_cursor)
        self.assertEqual([record.key for record in second.items], ["c", "d"])
        self.assertEqual((await self.store.search(self.scope, "Literal")).items, ())
        self.assertEqual(len((await self.store.list(self.scope, limit=1)).items), 1)

    async def test_memory_expiry_hides_records_and_fences_updates(self) -> None:
        """Expiry applies to get, list, search and conditional mutation alike."""
        record = await self.store.put(self.record(expires_at=time.time() + 0.1))
        await asyncio.sleep(0.15)
        self.assertIsNone(await self.store.get(self.scope, record.key))
        self.assertEqual((await self.store.list(self.scope)).items, ())
        self.assertEqual((await self.store.search(self.scope, "reports")).items, ())
        with self.assertRaises(MemoryConflictError):
            await self.store.put(self.record(), expected_revision=record.revision)

    async def test_memory_rejects_invalid_payloads_and_limits(self) -> None:
        """Validation never silently truncates private data or unbounded queries."""
        for changes in ({"content": ""}, {"content": "x" * 65537}, {"metadata": {"bad": float("nan")}}, {"expires_at": 0}):
            with self.assertRaises(ValueError):
                await self.store.put(self.record(**changes))
        for limit in (0, 101, True):
            with self.assertRaises(ValueError):
                await self.store.list(self.scope, limit=limit)
