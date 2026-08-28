from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from harnest.assets import (
    AssetMediaMetadata,
    AssetNotFoundError,
    AssetQuotaError,
    AssetRecord,
    AssetScope,
    AssetStore,
    AssetStoreError,
    AssetValidationError,
    MemoryAssetStore,
)


async def _chunks(*values: object):
    """Yield caller-supplied values to exercise the streaming boundary."""

    for value in values:
        yield value


async def _read(store: MemoryAssetStore, scope: AssetScope, asset_id: str) -> bytes:
    """Collect a streamed asset for concise behavioral assertions."""

    return b"".join(
        [chunk async for chunk in store.open(scope=scope, asset_id=asset_id)]
    )


class AssetDomainTests(unittest.TestCase):
    def test_scope_and_record_representations_redact_identifiers(self) -> None:
        scope = AssetScope(user_id="private-user", session_id="private-session")
        record = AssetRecord(
            asset_id="asset_private-token",
            scope=scope,
            media_type="image/png",
            size_bytes=4,
            created_at=datetime.now(timezone.utc),
        )

        rendered = repr((scope, record))

        self.assertNotIn("private-user", rendered)
        self.assertNotIn("private-session", rendered)
        self.assertNotIn("private-token", rendered)
        self.assertIn("image/png", rendered)

    def test_domain_validation_rejects_unsafe_shapes_without_echoing_values(self) -> None:
        secret = "secret-identifier"

        with self.assertRaises(ValueError) as scope_error:
            AssetScope(user_id=secret, session_id="")
        with self.assertRaises(ValueError) as media_error:
            AssetRecord(
                asset_id=secret,
                scope=AssetScope("user", "session"),
                media_type=f"invalid/{secret}/type",
                size_bytes=1,
                created_at=datetime.now(timezone.utc),
            )

        self.assertNotIn(secret, str(scope_error.exception))
        self.assertNotIn(secret, str(media_error.exception))

    def test_media_metadata_accepts_safe_inspected_properties(self) -> None:
        metadata = AssetMediaMetadata(
            width=640,
            height=480,
            duration_seconds=1.25,
            frame_count=30,
            channel_count=2,
            sample_rate_hz=48_000,
        )

        self.assertEqual(metadata.width, 640)
        with self.assertRaises(ValueError):
            AssetMediaMetadata(width=0)
        with self.assertRaises(ValueError):
            AssetMediaMetadata(duration_seconds=float("nan"))


class MemoryAssetStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.scope = AssetScope(user_id="user-a", session_id="session-a")
        self.other_user = AssetScope(user_id="user-b", session_id="session-a")
        self.other_session = AssetScope(user_id="user-a", session_id="session-b")

    async def test_store_satisfies_protocol_and_streams_bounded_chunks(self) -> None:
        store = MemoryAssetStore(
            max_asset_bytes=20,
            max_total_bytes=20,
            read_chunk_bytes=3,
            token_factory=lambda: "abcdefghijklmnop",
        )
        metadata = AssetMediaMetadata(width=2, height=3)

        record = await store.save(
            scope=self.scope,
            media_type=" IMAGE/PNG ",
            chunks=_chunks(b"abcd", bytearray(b"ef"), memoryview(b"ghi")),
            metadata=metadata,
        )
        streamed = [
            chunk
            async for chunk in store.open(
                scope=self.scope, asset_id=record.asset_id
            )
        ]

        self.assertIsInstance(store, AssetStore)
        self.assertEqual(record.asset_id, "asset_abcdefghijklmnop")
        self.assertEqual(record.media_type, "image/png")
        self.assertEqual(record.size_bytes, 9)
        self.assertEqual(record.metadata, metadata)
        self.assertEqual(streamed, [b"abc", b"d", b"ef", b"ghi"])
        self.assertEqual(store.total_bytes, 9)
        self.assertEqual(
            await store.stat(scope=self.scope, asset_id=record.asset_id), record
        )

    async def test_empty_asset_is_supported_without_consuming_quota(self) -> None:
        store = MemoryAssetStore(max_asset_bytes=1, max_total_bytes=1)

        record = await store.save(
            scope=self.scope,
            media_type="application/octet-stream",
            chunks=_chunks(),
        )

        self.assertEqual(record.size_bytes, 0)
        self.assertEqual(await _read(store, self.scope, record.asset_id), b"")
        self.assertEqual(store.total_bytes, 0)

    async def test_ownership_isolated_lookups_do_not_reveal_existence(self) -> None:
        store = MemoryAssetStore()
        record = await store.save(
            scope=self.scope, media_type="text/plain", chunks=_chunks(b"private")
        )

        for foreign_scope in (self.other_user, self.other_session):
            self.assertIsNone(
                await store.stat(scope=foreign_scope, asset_id=record.asset_id)
            )
            self.assertFalse(
                await store.delete(scope=foreign_scope, asset_id=record.asset_id)
            )
            with self.assertRaises(AssetNotFoundError) as foreign_error:
                await _read(store, foreign_scope, record.asset_id)
            with self.assertRaises(AssetNotFoundError) as absent_error:
                await _read(store, foreign_scope, "missing")
            self.assertEqual(foreign_error.exception.args, absent_error.exception.args)
            self.assertNotIn(record.asset_id, str(foreign_error.exception))

        self.assertEqual(await _read(store, self.scope, record.asset_id), b"private")

    async def test_per_asset_quota_failure_releases_reserved_capacity(self) -> None:
        store = MemoryAssetStore(max_asset_bytes=5, max_total_bytes=5)

        with self.assertRaises(AssetQuotaError) as error:
            await store.save(
                scope=self.scope,
                media_type="text/plain",
                chunks=_chunks(b"abc", b"def"),
            )

        self.assertNotIn("abcdef", str(error.exception))
        self.assertEqual(store.total_bytes, 0)
        record = await store.save(
            scope=self.scope,
            media_type="text/plain",
            chunks=_chunks(b"12345"),
        )
        self.assertEqual(record.size_bytes, 5)

    async def test_total_quota_is_reclaimed_by_delete_and_clear(self) -> None:
        store = MemoryAssetStore(max_asset_bytes=4, max_total_bytes=6)
        first = await store.save(
            scope=self.scope, media_type="text/plain", chunks=_chunks(b"1234")
        )

        with self.assertRaises(AssetQuotaError):
            await store.save(
                scope=self.scope, media_type="text/plain", chunks=_chunks(b"abc")
            )
        self.assertTrue(await store.delete(scope=self.scope, asset_id=first.asset_id))
        second = await store.save(
            scope=self.scope, media_type="text/plain", chunks=_chunks(b"abc")
        )
        self.assertEqual(store.total_bytes, 3)

        await store.clear()

        self.assertEqual(store.total_bytes, 0)
        self.assertIsNone(await store.stat(scope=self.scope, asset_id=second.asset_id))

    async def test_delete_scope_removes_only_one_session(self) -> None:
        store = MemoryAssetStore(max_asset_bytes=8, max_total_bytes=16)
        owned = await store.save(
            scope=self.scope, media_type="text/plain", chunks=_chunks(b"owned")
        )
        other = await store.save(
            scope=self.other_session,
            media_type="text/plain",
            chunks=_chunks(b"other"),
        )

        removed = await store.delete_scope(scope=self.scope)

        self.assertEqual(removed, 1)
        self.assertIsNone(await store.stat(scope=self.scope, asset_id=owned.asset_id))
        self.assertIsNotNone(
            await store.stat(scope=self.other_session, asset_id=other.asset_id)
        )

    async def test_failed_stream_does_not_retain_reserved_capacity(self) -> None:
        store = MemoryAssetStore(max_asset_bytes=8, max_total_bytes=8)

        async def failing_stream():
            yield b"secret"
            raise RuntimeError("upstream failed")

        with self.assertRaises(RuntimeError):
            await store.save(
                scope=self.scope,
                media_type="text/plain",
                chunks=failing_stream(),
            )
        record = await store.save(
            scope=self.scope,
            media_type="text/plain",
            chunks=_chunks(b"12345678"),
        )

        self.assertEqual(record.size_bytes, 8)

    async def test_invalid_chunk_and_identifier_errors_do_not_expose_values(self) -> None:
        secret = "unique/secret/payload"
        store = MemoryAssetStore(
            token_factory=lambda: secret,
            max_asset_bytes=100,
            max_total_bytes=100,
        )

        with self.assertRaises(AssetValidationError) as chunk_error:
            await store.save(
                scope=self.scope,
                media_type="text/plain",
                chunks=_chunks(secret),
            )
        with self.assertRaises(AssetStoreError) as identifier_error:
            await store.save(
                scope=self.scope,
                media_type="text/plain",
                chunks=_chunks(b"safe"),
            )

        self.assertNotIn(secret, str(chunk_error.exception))
        self.assertNotIn(secret, str(identifier_error.exception))

    async def test_upstream_stream_errors_are_sanitized(self) -> None:
        secret = "stream-contained-secret"
        store = MemoryAssetStore(max_asset_bytes=100, max_total_bytes=100)

        async def unsafe_stream():
            yield b"part"
            raise RuntimeError(secret)

        with self.assertRaises(AssetStoreError) as error:
            await store.save(
                scope=self.scope,
                media_type="text/plain",
                chunks=unsafe_stream(),
            )

        self.assertNotIn(secret, str(error.exception))
        record = await store.save(
            scope=self.scope,
            media_type="text/plain",
            chunks=_chunks(b"complete"),
        )
        self.assertEqual(record.size_bytes, 8)

    async def test_concurrent_uploads_reserve_against_one_total_ceiling(self) -> None:
        store = MemoryAssetStore(max_asset_bytes=4, max_total_bytes=7)
        first_ready = asyncio.Event()
        second_ready = asyncio.Event()
        release = asyncio.Event()

        async def gated_stream(ready: asyncio.Event):
            yield b"abc"
            ready.set()
            await release.wait()
            yield b"d"

        first_task = asyncio.create_task(
            store.save(
                scope=self.scope,
                media_type="text/plain",
                chunks=gated_stream(first_ready),
            )
        )
        second_task = asyncio.create_task(
            store.save(
                scope=self.scope,
                media_type="text/plain",
                chunks=gated_stream(second_ready),
            )
        )
        await asyncio.gather(first_ready.wait(), second_ready.wait())
        release.set()
        results = await asyncio.gather(first_task, second_task, return_exceptions=True)

        records = [result for result in results if isinstance(result, AssetRecord)]
        failures = [result for result in results if isinstance(result, AssetQuotaError)]
        self.assertEqual(len(records), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(store.total_bytes, 4)
        self.assertEqual(await _read(store, self.scope, records[0].asset_id), b"abcd")

    async def test_retention_expires_at_boundary_and_reclaims_quota(self) -> None:
        current = [datetime(2026, 8, 28, 12, tzinfo=timezone.utc)]
        store = MemoryAssetStore(
            max_asset_bytes=4,
            max_total_bytes=4,
            default_retention_seconds=30,
            clock=lambda: current[0],
        )
        record = await store.save(
            scope=self.scope, media_type="text/plain", chunks=_chunks(b"data")
        )
        self.assertEqual(record.expires_at, current[0] + timedelta(seconds=30))

        current[0] += timedelta(seconds=29)
        self.assertIsNotNone(await store.stat(scope=self.scope, asset_id=record.asset_id))
        current[0] += timedelta(seconds=1)
        self.assertIsNone(await store.stat(scope=self.scope, asset_id=record.asset_id))
        self.assertEqual(store.total_bytes, 0)
        self.assertEqual(await store.cleanup_expired(), 0)

    async def test_explicit_retention_overrides_default(self) -> None:
        current = [datetime(2026, 8, 28, tzinfo=timezone.utc)]
        store = MemoryAssetStore(
            default_retention_seconds=100, clock=lambda: current[0]
        )

        record = await store.save(
            scope=self.scope,
            media_type="text/plain",
            chunks=_chunks(b"a"),
            retention_seconds=5,
        )

        self.assertEqual(record.expires_at, current[0] + timedelta(seconds=5))

    async def test_delete_blocks_new_reads_but_existing_snapshot_finishes(self) -> None:
        store = MemoryAssetStore(read_chunk_bytes=2)
        record = await store.save(
            scope=self.scope,
            media_type="text/plain",
            chunks=_chunks(b"abcdef"),
        )
        reader = store.open(scope=self.scope, asset_id=record.asset_id)
        first = await anext(reader)

        self.assertTrue(await store.delete(scope=self.scope, asset_id=record.asset_id))
        remainder = b"".join([chunk async for chunk in reader])

        self.assertEqual(first + remainder, b"abcdef")
        with self.assertRaises(AssetNotFoundError):
            await _read(store, self.scope, record.asset_id)
        self.assertFalse(await store.delete(scope=self.scope, asset_id=record.asset_id))

    async def test_cancelled_upload_releases_capacity(self) -> None:
        store = MemoryAssetStore(max_asset_bytes=4, max_total_bytes=4)
        reserved = asyncio.Event()

        async def blocked_stream():
            yield b"1234"
            reserved.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(
            store.save(
                scope=self.scope,
                media_type="text/plain",
                chunks=blocked_stream(),
            )
        )
        await reserved.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        record = await store.save(
            scope=self.scope,
            media_type="text/plain",
            chunks=_chunks(b"abcd"),
        )
        self.assertEqual(record.size_bytes, 4)


if __name__ == "__main__":
    unittest.main()
