"""Opt-in live tests for user-defined database and S3 asset storage."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import ssl
from typing import Annotated, Any
import unittest
import urllib.request
import uuid

from pydantic import BaseModel

from harnest import Stored
from harnest.assets import (
    AssetMediaMetadata,
    AssetNotFoundError,
    AssetRecord,
    AssetScope,
    AssetStoreError,
)
from harnest.content import Image, ImageConstraints
from harnest.context import activate_context, context, create_agent_context
from harnest.runtime_langgraph import _materialize_model_message
from harnest.stored_media import stage_stored_media, stored_reference_value

try:
    import asyncpg
except ModuleNotFoundError:  # pragma: no cover - optional live dependency
    asyncpg = None

try:
    import boto3
    from botocore.client import Config
    from botocore.exceptions import ClientError
except ModuleNotFoundError:  # pragma: no cover - optional live dependency
    boto3 = None
    Config = None
    ClientError = Exception


_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
_PNG64 = base64.b64encode(_PNG).decode("ascii")
_POLICY = Stored(
    store="media",
    path="captures",
    expires_in=31,
    retention=timedelta(minutes=5),
)


class _StoredImage(BaseModel):
    image: Annotated[
        Image,
        ImageConstraints(
            media_types=frozenset({"image/png"}), max_bytes=1024
        ),
        _POLICY,
    ]


class PostgresAssetStorage:
    """Small user-defined database implementation used by the live contract."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: Any = None

    async def start(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=2)
        await self._pool.execute(
            """
            CREATE TABLE IF NOT EXISTS harnest_live_assets (
                asset_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                media_type TEXT NOT NULL,
                payload BYTEA NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                expires_at TIMESTAMPTZ,
                metadata JSONB
            )
            """
        )

    async def save(
        self,
        *,
        scope: AssetScope,
        media_type: str,
        chunks: AsyncIterable[bytes],
        metadata: AssetMediaMetadata | None = None,
        retention_seconds: float | None = None,
        path: str | None = None,
    ) -> AssetRecord:
        del path
        payload = b"".join([chunk async for chunk in chunks])
        created_at = datetime.now(timezone.utc)
        expires_at = _expiry(created_at, retention_seconds)
        asset_id = uuid.uuid4().hex
        await self._pool.execute(
            """
            INSERT INTO harnest_live_assets
                (asset_id, user_id, session_id, media_type, payload,
                 created_at, expires_at, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
            """,
            asset_id,
            scope.user_id,
            scope.session_id,
            media_type,
            payload,
            created_at,
            expires_at,
            _metadata_json(metadata),
        )
        return AssetRecord(
            asset_id=asset_id,
            scope=scope,
            media_type=media_type,
            size_bytes=len(payload),
            created_at=created_at,
            expires_at=expires_at,
            metadata=metadata,
        )

    async def stat(
        self, *, scope: AssetScope, asset_id: str
    ) -> AssetRecord | None:
        row = await self._pool.fetchrow(
            """
            SELECT asset_id, media_type, octet_length(payload) AS size_bytes,
                   created_at, expires_at, metadata
            FROM harnest_live_assets
            WHERE asset_id=$1 AND user_id=$2 AND session_id=$3
              AND (expires_at IS NULL OR expires_at > now())
            """,
            asset_id,
            scope.user_id,
            scope.session_id,
        )
        return _postgres_record(row, scope)

    async def open(
        self, *, scope: AssetScope, asset_id: str
    ) -> AsyncIterator[bytes]:
        payload = await self._pool.fetchval(
            """
            SELECT payload FROM harnest_live_assets
            WHERE asset_id=$1 AND user_id=$2 AND session_id=$3
              AND (expires_at IS NULL OR expires_at > now())
            """,
            asset_id,
            scope.user_id,
            scope.session_id,
        )
        if payload is None:
            raise AssetNotFoundError("asset not found")
        yield bytes(payload)

    async def delete(self, *, scope: AssetScope, asset_id: str) -> bool:
        status = await self._pool.execute(
            """
            DELETE FROM harnest_live_assets
            WHERE asset_id=$1 AND user_id=$2 AND session_id=$3
            """,
            asset_id,
            scope.user_id,
            scope.session_id,
        )
        return status == "DELETE 1"

    async def delete_scope(self, *, scope: AssetScope) -> int:
        status = await self._pool.execute(
            "DELETE FROM harnest_live_assets WHERE user_id=$1 AND session_id=$2",
            scope.user_id,
            scope.session_id,
        )
        return int(status.rpartition(" ")[2])

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()


class S3AssetStorage:
    """Small user-defined S3 implementation used by the live contract."""

    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
    ) -> None:
        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="us-east-1",
            verify=False,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    async def start(self) -> None:
        try:
            await asyncio.to_thread(self._client.create_bucket, Bucket=self._bucket)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                raise

    async def save(
        self,
        *,
        scope: AssetScope,
        media_type: str,
        chunks: AsyncIterable[bytes],
        metadata: AssetMediaMetadata | None = None,
        retention_seconds: float | None = None,
        path: str | None = None,
    ) -> AssetRecord:
        payload = b"".join([chunk async for chunk in chunks])
        created_at = datetime.now(timezone.utc)
        expires_at = _expiry(created_at, retention_seconds)
        asset_id = uuid.uuid4().hex
        key = _s3_key(scope, asset_id)
        attributes = {
            "created-at": created_at.isoformat(),
            "expires-at": expires_at.isoformat() if expires_at else "",
            "media": _metadata_json(metadata),
            "path": path or "",
        }
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=payload,
            ContentType=media_type,
            Metadata=attributes,
        )
        return AssetRecord(
            asset_id=asset_id,
            scope=scope,
            media_type=media_type,
            size_bytes=len(payload),
            created_at=created_at,
            expires_at=expires_at,
            metadata=metadata,
        )

    async def stat(
        self, *, scope: AssetScope, asset_id: str
    ) -> AssetRecord | None:
        try:
            head = await asyncio.to_thread(
                self._client.head_object,
                Bucket=self._bucket,
                Key=_s3_key(scope, asset_id),
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey"}:
                return None
            raise
        return _s3_record(head, scope, asset_id)

    async def open(
        self, *, scope: AssetScope, asset_id: str
    ) -> AsyncIterator[bytes]:
        if await self.stat(scope=scope, asset_id=asset_id) is None:
            raise AssetNotFoundError("asset not found")
        response = await asyncio.to_thread(
            self._client.get_object,
            Bucket=self._bucket,
            Key=_s3_key(scope, asset_id),
        )
        yield await asyncio.to_thread(response["Body"].read)
        response["Body"].close()

    async def delete(self, *, scope: AssetScope, asset_id: str) -> bool:
        if await self.stat(scope=scope, asset_id=asset_id) is None:
            return False
        await asyncio.to_thread(
            self._client.delete_object,
            Bucket=self._bucket,
            Key=_s3_key(scope, asset_id),
        )
        return True

    async def delete_scope(self, *, scope: AssetScope) -> int:
        prefix = _s3_scope_prefix(scope)
        response = await asyncio.to_thread(
            self._client.list_objects_v2, Bucket=self._bucket, Prefix=prefix
        )
        keys = [item["Key"] for item in response.get("Contents", [])]
        for key in keys:
            await asyncio.to_thread(
                self._client.delete_object, Bucket=self._bucket, Key=key
            )
        return len(keys)

    async def signed_url(
        self, *, scope: AssetScope, asset_id: str, expires_in: float
    ) -> str:
        if await self.stat(scope=scope, asset_id=asset_id) is None:
            raise AssetNotFoundError("asset not found")
        return await asyncio.to_thread(
            self._client.generate_presigned_url,
            "get_object",
            Params={"Bucket": self._bucket, "Key": _s3_key(scope, asset_id)},
            ExpiresIn=int(expires_in),
        )

    async def close(self) -> None:
        self._client.close()


class LiveAssetStorageTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(
        os.getenv("HARNEST_TEST_ASSET_POSTGRES_URL") and asyncpg is not None,
        "set HARNEST_TEST_ASSET_POSTGRES_URL for the live database asset test",
    )
    async def test_custom_database_storage_through_harnest_contract(self):
        storage = PostgresAssetStorage(
            os.environ["HARNEST_TEST_ASSET_POSTGRES_URL"]
        )
        await _exercise_storage(self, storage, expect_url=False)

    @unittest.skipUnless(
        os.getenv("HARNEST_TEST_ASSET_S3_ENDPOINT") and boto3 is not None,
        "set HARNEST_TEST_ASSET_S3_ENDPOINT for the live S3 asset test",
    )
    async def test_s3_storage_and_presigned_model_url(self):
        storage = S3AssetStorage(
            endpoint=os.environ["HARNEST_TEST_ASSET_S3_ENDPOINT"],
            access_key=os.environ["HARNEST_TEST_ASSET_S3_ACCESS_KEY"],
            secret_key=os.environ["HARNEST_TEST_ASSET_S3_SECRET_KEY"],
            bucket=os.environ["HARNEST_TEST_ASSET_S3_BUCKET"],
        )
        await _exercise_storage(self, storage, expect_url=True)


async def _exercise_storage(
    case: unittest.TestCase, storage: Any, *, expect_url: bool
) -> None:
    scope = AssetScope(f"user-{uuid.uuid4().hex}", f"session-{uuid.uuid4().hex}")
    other = AssetScope("other-user", scope.session_id)
    await storage.start()
    try:
        staged = await stage_stored_media(
            _StoredImage(image=Image(data=_PNG64, mediaType="image/png")),
            stores={"media": storage},
            scope=scope,
        )
        active = create_agent_context(
            framework="langgraph",
            agent_name="live-storage",
            invocation_id=uuid.uuid4().hex,
            user_id=scope.user_id,
            session_id=scope.session_id,
            metadata={},
            resources={},
            asset_stores={"media": storage},
        )
        with activate_context(active):
            case.assertIsNone(staged.image.data)
            case.assertEqual(staged.image.store, "media")
            case.assertEqual(await context.assets.get(staged.image), _PNG)
            await _assert_url_mode(
                case, context.assets, staged.image, storage, scope, expect_url
            )
            case.assertTrue(await context.assets.delete(staged.image))
        case.assertIsNone(
            await storage.stat(scope=other, asset_id=staged.image.asset_id)
        )
        case.assertIsNone(
            await storage.stat(scope=scope, asset_id=staged.image.asset_id)
        )
    finally:
        await storage.delete_scope(scope=scope)
        await storage.close()


async def _assert_url_mode(
    case: unittest.TestCase,
    assets: Any,
    image: Image,
    storage: Any,
    scope: AssetScope,
    expect_url: bool,
) -> None:
    if not expect_url:
        with case.assertRaisesRegex(
            AssetStoreError, "does not support temporary URLs"
        ):
            await assets.url(image, expires_in=31)
        await _assert_model_url_failure(case, image, storage, scope)
        return
    url = await assets.url(image, expires_in=31)
    case.assertTrue(url.startswith("https://"))
    model_url = await _model_url(image, storage, scope)
    case.assertTrue(model_url.startswith("https://"))
    await _assert_url_payload(case, url)
    await _assert_url_payload(case, model_url)


async def _assert_url_payload(case: unittest.TestCase, url: str) -> None:
    tls = ssl.create_default_context()
    tls.check_hostname = False
    tls.verify_mode = ssl.CERT_NONE
    payload = await asyncio.to_thread(
        lambda: urllib.request.urlopen(url, context=tls, timeout=10).read()
    )
    case.assertEqual(payload, _PNG)


async def _assert_model_url_failure(
    case: unittest.TestCase, image: Image, storage: Any, scope: AssetScope
) -> None:
    with case.assertRaisesRegex(RuntimeError, "cannot generate model URLs"):
        await _model_url(image, storage, scope)


async def _model_url(image: Image, storage: Any, scope: AssetScope) -> str:
    class Message:
        def __init__(self, content: Any) -> None:
            self.content = content

        def model_copy(self, *, update: dict[str, Any]) -> Any:
            return Message(update["content"])

    marker = stored_reference_value(image, _POLICY)
    projected, _ = await _materialize_model_message(
        Message(json.dumps({"image": marker})),
        stores={"media": storage},
        scope=scope,
    )
    return projected.content[1]["url"]


def _expiry(created_at: datetime, seconds: float | None) -> datetime | None:
    return None if seconds is None else created_at + timedelta(seconds=seconds)


def _metadata_json(metadata: AssetMediaMetadata | None) -> str:
    return json.dumps(asdict(metadata) if metadata is not None else {})


def _metadata(value: Any) -> AssetMediaMetadata | None:
    if isinstance(value, str):
        value = json.loads(value)
    return AssetMediaMetadata(**value) if value else None


def _postgres_record(row: Any, scope: AssetScope) -> AssetRecord | None:
    if row is None:
        return None
    return AssetRecord(
        asset_id=row["asset_id"],
        scope=scope,
        media_type=row["media_type"],
        size_bytes=row["size_bytes"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        metadata=_metadata(row["metadata"]),
    )


def _s3_record(head: dict[str, Any], scope: AssetScope, asset_id: str) -> AssetRecord:
    attributes = head["Metadata"]
    created_at = datetime.fromisoformat(attributes["created-at"])
    expires_at = (
        datetime.fromisoformat(attributes["expires-at"])
        if attributes.get("expires-at")
        else None
    )
    return AssetRecord(
        asset_id=asset_id,
        scope=scope,
        media_type=head["ContentType"],
        size_bytes=head["ContentLength"],
        created_at=created_at,
        expires_at=expires_at,
        metadata=_metadata(attributes.get("media")),
    )


def _s3_scope_prefix(scope: AssetScope) -> str:
    owner = hashlib.sha256(scope.user_id.encode()).hexdigest()
    session = hashlib.sha256(scope.session_id.encode()).hexdigest()
    return f"harnest/{owner}/{session}/"


def _s3_key(scope: AssetScope, asset_id: str) -> str:
    return f"{_s3_scope_prefix(scope)}{asset_id}"


if __name__ == "__main__":
    unittest.main()
