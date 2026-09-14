"""Explicit memory on existing PostgreSQL databases without vector extensions."""

from contextlib import asynccontextmanager
from dataclasses import replace
import hashlib
import json
import time
from typing import Any, AsyncIterator, Mapping

from .memory import MemoryCleanupPage, MemoryPage, MemoryRecord, MemoryScope
from .memory_provider import MemoryProvider, decode_record, encode_record, revision_check
from .store_postgres import _create_pool

_SCHEMA = """
CREATE TABLE IF NOT EXISTS harnest_memories (
 application_id text NOT NULL, user_id text NOT NULL, namespace text NOT NULL,
 key text COLLATE "C" NOT NULL, record jsonb NOT NULL,
 PRIMARY KEY(application_id,user_id,namespace,key)
);
"""


class PostgresMemoryStore(MemoryProvider):
    """Persist deliberate memories in a Harnest-owned table in your database."""

    def __init__(self, dsn: str, *, pool_options: Mapping[str, Any] | None = None, setup_schema: bool = True) -> None:
        """Retain configuration; importing or constructing does not connect."""
        self._dsn = dsn
        self._options = dict(pool_options or {})
        self._setup_schema = setup_schema
        self._pool: Any = None

    async def start(self) -> None:
        """Open an owned pool and serialize schema setup across replicas."""
        if self._pool is not None:
            return
        self._pool = await _create_pool(self._dsn, self._options)
        try:
            async with self._pool.acquire() as connection, connection.transaction():
                await connection.execute("SELECT pg_advisory_xact_lock(681274631)")
                if self._setup_schema:
                    await connection.execute(_SCHEMA)
                else:
                    await connection.fetch("SELECT application_id,user_id,namespace,key,record FROM harnest_memories LIMIT 0")
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        """Close only this provider's connection pool."""
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Any]:
        """Reject unstarted access before acquiring database ownership."""
        if self._pool is None:
            raise RuntimeError("memory provider is not started")
        async with self._pool.acquire() as connection:
            yield connection

    async def _put(self, record: MemoryRecord, expected: str | None) -> MemoryRecord:
        """Fence insert/update/delete races under the same per-key database lock."""
        async with self._connection() as connection, connection.transaction():
            current = await _locked_record(connection, record.scope, record.key)
            revision_check(expected, current)
            if current is not None:
                record = replace(record, created_at=current.created_at)
            await connection.execute(
                "INSERT INTO harnest_memories VALUES($1,$2,$3,$4,$5::jsonb) "
                "ON CONFLICT(application_id,user_id,namespace,key) DO UPDATE SET record=excluded.record",
                *_identity(record.scope, record.key), encode_record(record),
            )
        return record

    async def _read(self, scope: MemoryScope, key: str | None, query: str | None, limit: int, after: str | None) -> MemoryPage:
        """Apply scope, expiry, literal search and pagination inside PostgreSQL."""
        async with self._connection() as connection:
            rows = await connection.fetch(
                "SELECT record::text FROM harnest_memories WHERE application_id=$1 AND user_id=$2 AND namespace=$3 "
                "AND ($4::text IS NULL OR key=$4) AND ($5::text IS NULL OR position($5 in record->>'content')>0) "
                "AND ($6::text IS NULL OR key>$6 COLLATE \"C\") "
                "AND ((record->>'expires_at')::double precision IS NULL OR (record->>'expires_at')::double precision>$7) "
                "ORDER BY key LIMIT $8",
                scope.application_id, scope.user_id, scope.namespace, key, query, after, time.time(), limit + 1,
            )
        records = tuple(decode_record(row["record"]) for row in rows)
        return MemoryPage(records[:limit], records[limit - 1].key if len(records) > limit else None)

    async def _delete(self, scope: MemoryScope, key: str, expected: str | None) -> bool:
        """Delete a revision and its primary index entry in one transaction."""
        async with self._connection() as connection, connection.transaction():
            current = await _locked_record(connection, scope, key)
            revision_check(expected, current)
            await connection.execute(
                "DELETE FROM harnest_memories WHERE application_id=$1 AND user_id=$2 AND namespace=$3 AND key=$4",
                *_identity(scope, key),
            )
        return current is not None

    async def _delete_all(self, scope: MemoryScope) -> int:
        """Delete a namespace in one fully scoped datastore statement."""
        return await self._erase(scope, scope.namespace)

    async def _delete_user(self, scope: MemoryScope) -> int:
        """Delete all namespaces for one application/user without loading records."""
        return await self._erase(scope, None)

    async def _erase(self, scope: MemoryScope, namespace: str | None) -> int:
        """Bind trusted ownership even when the namespace predicate is omitted."""
        async with self._connection() as connection:
            status = await connection.execute(
                "DELETE FROM harnest_memories WHERE application_id=$1 AND user_id=$2 "
                "AND ($3::text IS NULL OR namespace=$3)", scope.application_id, scope.user_id, namespace,
            )
        return int(status.split()[-1])

    async def _purge_expired(self, scope: MemoryScope, limit: int, after: str | None) -> MemoryCleanupPage:
        """Select and sweep a bounded page while rechecking expiry under row locks."""
        async with self._connection() as connection, connection.transaction():
            keys = await connection.fetch(
                'SELECT key FROM harnest_memories WHERE application_id=$1 AND user_id=$2 AND namespace=$3 '
                'AND ($4::text IS NULL OR key>$4 COLLATE "C") ORDER BY key LIMIT $5',
                scope.application_id, scope.user_id, scope.namespace, after, limit + 1,
            )
            status = await connection.execute(
                "DELETE FROM harnest_memories WHERE application_id=$1 AND user_id=$2 AND namespace=$3 "
                "AND key=ANY($4::text[]) AND (record->>'expires_at')::double precision<=$5",
                scope.application_id, scope.user_id, scope.namespace,
                [row["key"] for row in keys[:limit]], time.time(),
            )
        return MemoryCleanupPage(int(status.split()[-1]), keys[limit - 1]["key"] if len(keys) > limit else None)


def _identity(scope: MemoryScope, key: str) -> tuple[str, str, str, str]:
    """Bind all datastore predicates to the explicit trusted identity."""
    return scope.application_id, scope.user_id, scope.namespace, key


async def _locked_record(connection: Any, scope: MemoryScope, key: str) -> MemoryRecord | None:
    """Lock absent keys too, so stale writers cannot resurrect deleted revisions."""
    identity = _identity(scope, key)
    lock = int.from_bytes(hashlib.sha256(json.dumps(identity).encode()).digest()[:8], signed=True)
    await connection.execute("SELECT pg_advisory_xact_lock($1)", lock)
    raw = await connection.fetchval(
        "SELECT record::text FROM harnest_memories WHERE application_id=$1 AND user_id=$2 AND namespace=$3 AND key=$4",
        *identity,
    )
    if raw is None:
        return None
    record = decode_record(raw)
    return record if record.expires_at is None or record.expires_at > time.time() else None
