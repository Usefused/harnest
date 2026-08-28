"""PostgreSQL-backed Harnest session and checkpoint storage."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import importlib
import json
from typing import Any, Literal

from ._json import json_value
from .checkpoint import (
    CheckpointConflictError,
    CheckpointRecord,
    CheckpointWrite,
    HarnestStore,
    PendingAction,
    RunRecord,
    RunStatus,
    _require_fields,
    _validate_same_run,
    _validate_transition,
)
from .logging import get_logger
from .runtime_contract import SessionConflictError, SessionRecord
from .session import SessionLease, _require_list_options
from .store_postgres_schema import SCHEMA_LOCK, SCHEMA_SQL, SCHEMA_VERSION


_AUDIT = get_logger("store.audit")


class PostgresStore(HarnestStore):
    """Durable combined store using asyncpg and one Harnest-owned schema.

    The driver is imported lazily by :meth:`start`, keeping module discovery
    independent from database connectivity.
    """

    def __init__(
        self,
        dsn: str,
        *,
        pool_options: Mapping[str, Any] | None = None,
        setup_schema: bool = True,
        _pool: Any = None,
    ) -> None:
        """Configure schema ownership and preserve ownership of injected pools."""

        _require_text(dsn, "dsn")
        self._dsn = dsn
        self._pool_options = dict(pool_options or {})
        self._setup_schema = setup_schema
        self._pool = _pool
        self._owns_pool = _pool is None

    async def start(self) -> None:
        """Open the pool and create or validate the Harnest-owned schema."""

        if self._pool is None:
            self._pool = await _create_pool(self._dsn, self._pool_options)
        if self._setup_schema:
            await self._bootstrap_schema()
        else:
            await self._validate_schema()

    async def create(
        self, *, session_id: str, user_id: str, state: Mapping[str, Any]
    ) -> SessionRecord:
        """Create one tenant-scoped session without overwriting conflicts."""

        _require_text(session_id, "session_id")
        _require_text(user_id, "user_id")
        query = """
        INSERT INTO harnest_sessions(user_id, session_id, state)
        VALUES ($1, $2, $3::jsonb)
        ON CONFLICT DO NOTHING
        RETURNING *
        """
        try:
            async with self._connection() as connection:
                row = await connection.fetchrow(
                    query, user_id, session_id, _json_dump(state)
                )
        except Exception:
            _audit("session.create", "user", "failed", "postgres")
            raise
        if row is None:
            _audit("session.create", "user", "failed", "postgres")
            raise SessionConflictError("session already exists")
        _audit("session.create", "user", "committed", "postgres")
        return _session_from_row(row)

    async def get(
        self, *, session_id: str, user_id: str
    ) -> SessionRecord | None:
        async with self._connection() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM harnest_sessions WHERE user_id=$1 AND session_id=$2",
                user_id,
                session_id,
            )
        return None if row is None else _session_from_row(row)

    async def list(
        self,
        *,
        user_id: str,
        after: str | None = None,
        limit: int | None = None,
    ) -> Sequence[SessionRecord]:
        """Apply tenant filtering and optional keyset pagination in PostgreSQL."""

        _require_text(user_id, "user_id")
        _require_list_options(after, limit)
        async with self._connection() as connection:
            rows = await connection.fetch(
                """
                SELECT * FROM harnest_sessions
                WHERE user_id=$1
                  AND ($2::text IS NULL OR session_id > $2)
                ORDER BY session_id
                LIMIT $3
                """,
                user_id,
                after,
                limit,
            )
        return tuple(_session_from_row(row) for row in rows)

    async def update(
        self,
        *,
        session_id: str,
        user_id: str,
        state_delta: Mapping[str, Any],
    ) -> SessionRecord | None:
        """Atomically merge a state delta in PostgreSQL."""

        query = """
        UPDATE harnest_sessions
        SET state=state || $3::jsonb, updated_at=now()
        WHERE user_id=$1 AND session_id=$2
        RETURNING *
        """
        try:
            async with self._locked_session_connection(
                user_id, session_id
            ) as connection:
                row = await connection.fetchrow(
                    query, user_id, session_id, _json_dump(state_delta)
                )
        except Exception:
            _audit("session.update", "agent", "failed", "postgres")
            raise
        if row is not None:
            _audit("session.update", "agent", "committed", "postgres")
            return _session_from_row(row)
        return None

    async def delete(self, *, session_id: str, user_id: str) -> bool:
        """Delete one tenant-scoped session and report whether it existed."""

        try:
            async with self._locked_session_connection(
                user_id, session_id
            ) as connection:
                row = await connection.fetchrow(
                    """
                    DELETE FROM harnest_sessions
                    WHERE user_id=$1 AND session_id=$2 RETURNING session_id
                    """,
                    user_id,
                    session_id,
                )
        except Exception:
            _audit("session.delete", "user", "failed", "postgres")
            raise
        if row is None:
            return False
        _audit("session.delete", "user", "committed", "postgres")
        return True

    @asynccontextmanager
    async def acquire(
        self, *, session_id: str, user_id: str
    ) -> AsyncIterator[SessionLease]:
        """Hold a cross-replica advisory lock for one session execution."""

        async with self._locked_session_connection(
            user_id, session_id
        ) as connection:
            row = await connection.fetchrow(
                """
                SELECT * FROM harnest_sessions
                WHERE user_id=$1 AND session_id=$2
                """,
                user_id,
                session_id,
            )
            if row is None:
                raise KeyError("session not found")
            yield _PostgresLease(connection, _session_from_row(row))

    async def begin_run(
        self,
        *,
        application_id: str,
        user_id: str,
        session_id: str,
        run_id: str,
        framework: Literal["adk", "langgraph"],
    ) -> RunRecord:
        """Create an idempotent run while allowing one active run per session."""

        _require_fields(application_id, user_id, session_id, run_id)
        query = """
        INSERT INTO harnest_runs(
            run_id, application_id, user_id, session_id, framework, status
        ) VALUES ($1, $2, $3, $4, $5, 'running')
        ON CONFLICT DO NOTHING RETURNING *
        """
        try:
            async with self._connection() as connection:
                row = await connection.fetchrow(
                    query, run_id, application_id, user_id, session_id, framework
                )
                if row is None:
                    row = await connection.fetchrow(
                        "SELECT * FROM harnest_runs WHERE run_id=$1", run_id
                    )
        except Exception:
            _audit("checkpoint.run_started", "agent", "failed", "postgres")
            raise
        if row is None:
            _audit("checkpoint.run_started", "agent", "failed", "postgres")
            raise CheckpointConflictError("session already has an active run")
        record = _run_from_row(row)
        try:
            _validate_same_run(
                record, application_id, user_id, session_id, framework
            )
        except CheckpointConflictError:
            _audit("checkpoint.run_started", "agent", "failed", "postgres")
            raise
        _audit("checkpoint.run_started", "agent", "committed", "postgres")
        return record

    async def get_run(self, *, run_id: str) -> RunRecord | None:
        async with self._connection() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM harnest_runs WHERE run_id=$1", run_id
            )
        return None if row is None else _run_from_row(row)

    async def get_checkpoint(
        self,
        *,
        run_id: str,
        checkpoint_id: str | None = None,
        namespace: str = "",
    ) -> CheckpointRecord | None:
        """Load an exact or newest checkpoint entirely in PostgreSQL."""

        if checkpoint_id is None:
            query = """
            SELECT * FROM harnest_checkpoints
            WHERE run_id=$1 AND namespace=$2
            ORDER BY revision DESC, checkpoint_id DESC LIMIT 1
            """
            arguments = (run_id, namespace)
        else:
            query = """
            SELECT * FROM harnest_checkpoints
            WHERE run_id=$1 AND namespace=$2 AND checkpoint_id=$3
            """
            arguments = (run_id, namespace, checkpoint_id)
        async with self._connection() as connection:
            row = await connection.fetchrow(query, *arguments)
        return None if row is None else _checkpoint_from_row(row)

    async def list_checkpoints(
        self,
        *,
        run_id: str,
        namespace: str | None = None,
        before: str | None = None,
        limit: int = 20,
    ) -> AsyncIterator[CheckpointRecord]:
        """Push checkpoint filtering, ordering, cursor, and limit into SQL."""

        _require_limit(limit)
        query = """
        SELECT * FROM harnest_checkpoints
        WHERE run_id=$1
          AND ($2::text IS NULL OR namespace=$2)
          AND ($3::text IS NULL OR checkpoint_id < $3)
        ORDER BY revision DESC, checkpoint_id DESC
        LIMIT $4
        """
        async with self._connection() as connection:
            rows = await connection.fetch(query, run_id, namespace, before, limit)
        for row in rows:
            yield _checkpoint_from_row(row)

    async def put(
        self, checkpoint: CheckpointRecord, *, expected_revision: int | None
    ) -> CheckpointRecord:
        """Allocate a serialized revision and write only on matching CAS state."""

        query = """
        INSERT INTO harnest_checkpoints(
            run_id, namespace, checkpoint_id, framework, type_name, payload,
            metadata_type, metadata, versions_type, versions,
            parent_checkpoint_id, revision, created_at
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$13,$12::timestamptz)
        ON CONFLICT (run_id, namespace, checkpoint_id) DO UPDATE SET
            framework=EXCLUDED.framework, type_name=EXCLUDED.type_name,
            payload=EXCLUDED.payload, metadata_type=EXCLUDED.metadata_type,
            metadata=EXCLUDED.metadata, versions_type=EXCLUDED.versions_type,
            versions=EXCLUDED.versions,
            parent_checkpoint_id=EXCLUDED.parent_checkpoint_id,
            revision=EXCLUDED.revision
        RETURNING *
        """
        try:
            async with self._connection() as connection:
                async with connection.transaction():
                    await _lock_active_run(connection, checkpoint.run_id)
                    await _validate_checkpoint_revision(
                        connection, checkpoint, expected_revision
                    )
                    revision = await connection.fetchval(
                        """
                        SELECT COALESCE(MAX(revision), -1) + 1
                        FROM harnest_checkpoints
                        WHERE run_id=$1 AND namespace=$2
                        """,
                        checkpoint.run_id,
                        checkpoint.namespace,
                    )
                    row = await connection.fetchrow(
                        query, *_checkpoint_values(checkpoint), revision
                    )
        except Exception:
            _audit("checkpoint.saved", "agent", "failed", "postgres")
            raise
        _audit("checkpoint.saved", "agent", "committed", "postgres")
        return _checkpoint_from_row(row)

    async def put_writes(
        self,
        *,
        run_id: str,
        checkpoint_id: str,
        writes: Sequence[CheckpointWrite],
    ) -> None:
        """Insert pending writes as one set and ignore existing identities."""

        if not writes:
            return
        query = """
        INSERT INTO harnest_checkpoint_writes(
            run_id, checkpoint_id, task_id, channel, type_name, payload, task_path
        )
        SELECT $1, $2, item.*
        FROM UNNEST(
            $3::text[], $4::text[], $5::text[], $6::bytea[], $7::text[]
        ) AS item(task_id, channel, type_name, payload, task_path)
        WHERE EXISTS (
            SELECT 1 FROM harnest_runs
            WHERE run_id=$1 AND status IN ('running', 'waiting')
        )
        ON CONFLICT DO NOTHING
        """
        columns = tuple(zip(*(_write_values(item) for item in writes)))
        try:
            async with self._connection() as connection:
                status = await connection.execute(
                    query, run_id, checkpoint_id, *columns
                )
            if status == "INSERT 0 0":
                await self._require_active_run(run_id)
        except Exception:
            _audit("checkpoint.writes_saved", "agent", "failed", "postgres")
            raise
        _audit("checkpoint.writes_saved", "agent", "committed", "postgres")

    async def get_writes(
        self, *, run_id: str, checkpoint_id: str
    ) -> Sequence[CheckpointWrite]:
        async with self._connection() as connection:
            rows = await connection.fetch(
                """
                SELECT task_id, channel, type_name, payload, task_path
                FROM harnest_checkpoint_writes
                WHERE run_id=$1 AND checkpoint_id=$2
                ORDER BY task_id, channel
                """,
                run_id,
                checkpoint_id,
            )
        return tuple(_write_from_row(row) for row in rows)

    async def get_writes_batch(
        self, *, run_id: str, checkpoint_ids: Sequence[str]
    ) -> Mapping[str, Sequence[CheckpointWrite]]:
        """Fetch pending writes for several checkpoints in one query."""

        if not checkpoint_ids:
            return {}
        async with self._connection() as connection:
            rows = await connection.fetch(
                """
                SELECT checkpoint_id, task_id, channel, type_name, payload, task_path
                FROM harnest_checkpoint_writes
                WHERE run_id=$1 AND checkpoint_id=ANY($2::text[])
                ORDER BY checkpoint_id, task_id, channel
                """,
                run_id,
                list(checkpoint_ids),
            )
        grouped: dict[str, list[CheckpointWrite]] = {}
        for row in rows:
            grouped.setdefault(row["checkpoint_id"], []).append(
                _write_from_row(row)
            )
        return {
            key: tuple(grouped.get(key, ())) for key in checkpoint_ids
        }

    async def transition(
        self,
        *,
        run_id: str,
        expected_status: RunStatus,
        status: RunStatus,
        pending_action: PendingAction | None = None,
    ) -> RunRecord:
        """Apply a validated status transition with one SQL compare-and-swap."""

        _validate_transition(expected_status, status, pending_action)
        query = """
        UPDATE harnest_runs SET
            status=$3, pending_action=$4::jsonb,
            revision=revision + 1, updated_at=now()
        WHERE run_id=$1 AND status=$2 RETURNING *
        """
        try:
            async with self._connection() as connection:
                row = await connection.fetchrow(
                    query,
                    run_id,
                    expected_status,
                    status,
                    _pending_dump(pending_action),
                )
        except Exception:
            _audit("checkpoint.transition", "agent", "failed", "postgres")
            raise
        if row is None:
            try:
                await self._raise_transition_conflict(run_id)
            except Exception:
                _audit("checkpoint.transition", "agent", "failed", "postgres")
                raise
        record = _run_from_row(row)
        _audit("checkpoint.transition", "agent", "committed", "postgres")
        return record

    async def delete_run(self, *, run_id: str) -> None:
        """Delete a run and cascade its private checkpoint data."""

        try:
            async with self._connection() as connection:
                row = await connection.fetchrow(
                    "DELETE FROM harnest_runs WHERE run_id=$1 RETURNING *", run_id
                )
        except Exception:
            _audit("checkpoint.run_deleted", "user", "failed", "postgres")
            raise
        if row is not None:
            _audit("checkpoint.run_deleted", "user", "committed", "postgres")

    async def close(self) -> None:
        """Close only pools created and owned by this store."""

        pool, self._pool = self._pool, None
        if pool is not None and self._owns_pool:
            await pool.close()

    async def _bootstrap_schema(self) -> None:
        """Serialize automatic schema bootstrap across concurrent replicas."""

        async with self._connection() as connection:
            async with connection.transaction():
                # One transaction-scoped lock prevents replicas racing schema
                # bootstrap while still releasing automatically on failure.
                await connection.execute(
                    "SELECT pg_advisory_xact_lock($1)", SCHEMA_LOCK
                )
                await connection.execute(SCHEMA_SQL)
                await _validate_postgres_version(connection)

    async def _validate_schema(self) -> None:
        """Validate pre-provisioned schema when automatic setup is disabled."""

        async with self._connection() as connection:
            await _validate_postgres_version(connection)

    async def _require_active_run(self, run_id: str) -> RunRecord:
        """Explain a zero-row set write as missing or terminal run state."""

        record = await self.get_run(run_id=run_id)
        if record is None:
            raise KeyError("checkpoint run not found")
        if record.status not in {"running", "waiting"}:
            raise CheckpointConflictError("checkpoint run is already terminal")
        return record

    async def _raise_transition_conflict(self, run_id: str) -> None:
        """Distinguish a missing run from a stale status transition."""

        if await self.get_run(run_id=run_id) is None:
            raise KeyError("checkpoint run not found")
        raise CheckpointConflictError("checkpoint run status changed")

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Any]:
        """Centralize the started-store guard for every database operation."""

        if self._pool is None:
            raise RuntimeError("PostgresStore.start() must be called first")
        async with self._pool.acquire() as connection:
            yield connection

    @asynccontextmanager
    async def _locked_session_connection(
        self, user_id: str, session_id: str
    ) -> AsyncIterator[Any]:
        """Keep the advisory lock and its owning connection at one boundary."""

        key = _lock_key(user_id, session_id)
        async with self._connection() as connection:
            # A dedicated connection keeps the advisory lock scoped to exactly
            # one invocation without holding a transaction open around a model.
            await connection.execute("SELECT pg_advisory_lock($1)", key)
            try:
                yield connection
            finally:
                await connection.execute("SELECT pg_advisory_unlock($1)", key)


class _PostgresLease:
    def __init__(self, connection: Any, record: SessionRecord) -> None:
        self._connection = connection
        self._record = record

    @property
    def record(self) -> SessionRecord:
        return self._record

    async def patch_state(self, delta: Mapping[str, Any]) -> None:
        state = {**dict(self._record.state), **json_value(delta)}
        await self.replace_state(state)

    async def replace_state(self, state: Mapping[str, Any]) -> None:
        """Replace leased state on the already locked connection."""

        try:
            row = await self._connection.fetchrow(
                """
                UPDATE harnest_sessions SET state=$3::jsonb, updated_at=now()
                WHERE user_id=$1 AND session_id=$2 RETURNING *
                """,
                self._record.user_id,
                self._record.id,
                _json_dump(state),
            )
        except Exception:
            _audit("session.lease_update", "agent", "failed", "postgres")
            raise
        if row is None:
            _audit("session.lease_update", "agent", "failed", "postgres")
            raise KeyError("session not found")
        self._record = _session_from_row(row)
        _audit("session.lease_update", "agent", "committed", "postgres")


async def _create_pool(dsn: str, options: Mapping[str, Any]) -> Any:
    """Import asyncpg only when this backend is selected and create its pool."""

    try:
        asyncpg = importlib.import_module("asyncpg")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "PostgresStore requires the 'asyncpg' package; install harnest[postgres]"
        ) from exc
    return await asyncpg.create_pool(dsn=dsn, **dict(options))


async def _validate_postgres_version(connection: Any) -> None:
    """Fail startup before serving when the database schema is incompatible."""

    version = await connection.fetchval(
        """
        SELECT version FROM harnest_schema_migrations WHERE component='store'
        """
    )
    if version != SCHEMA_VERSION:
        raise RuntimeError(
            f"unsupported Harnest store schema {version!r}; expected {SCHEMA_VERSION}"
        )


def _session_from_row(row: Mapping[str, Any]) -> SessionRecord:
    return SessionRecord(
        id=row["session_id"],
        user_id=row["user_id"],
        state=_json_load(row["state"]),
        created_at=_iso(row["created_at"]),
        updated_at=_iso(row["updated_at"]),
    )


def _run_from_row(row: Mapping[str, Any]) -> RunRecord:
    """Restore the typed pending action at the database serialization boundary."""

    pending = _json_load(row.get("pending_action"))
    return RunRecord(
        application_id=row["application_id"],
        user_id=row["user_id"],
        session_id=row["session_id"],
        run_id=row["run_id"],
        framework=row["framework"],
        status=row["status"],
        revision=row["revision"],
        pending_action=None if pending is None else PendingAction(**pending),
        created_at=_iso(row["created_at"]),
        updated_at=_iso(row["updated_at"]),
    )


def _checkpoint_from_row(row: Mapping[str, Any]) -> CheckpointRecord:
    """Copy driver buffers so opaque checkpoint payloads outlive the query."""

    return CheckpointRecord(
        run_id=row["run_id"],
        checkpoint_id=row["checkpoint_id"],
        namespace=row["namespace"],
        framework=row["framework"],
        type_name=row["type_name"],
        payload=bytes(row["payload"]),
        metadata_type=row["metadata_type"],
        metadata=bytes(row["metadata"]),
        versions_type=row["versions_type"],
        versions=bytes(row["versions"]),
        parent_checkpoint_id=row["parent_checkpoint_id"],
        revision=row["revision"],
        created_at=_iso(row["created_at"]),
    )


def _write_from_row(row: Mapping[str, Any]) -> CheckpointWrite:
    return CheckpointWrite(
        task_id=row["task_id"],
        channel=row["channel"],
        type_name=row["type_name"],
        payload=bytes(row["payload"]),
        task_path=row["task_path"],
    )


def _checkpoint_values(value: CheckpointRecord) -> tuple[Any, ...]:
    """Preserve the positional contract shared with the checkpoint upsert."""

    return (
        value.run_id,
        value.namespace,
        value.checkpoint_id,
        value.framework,
        value.type_name,
        value.payload,
        value.metadata_type,
        value.metadata,
        value.versions_type,
        value.versions,
        value.parent_checkpoint_id,
        _parse_timestamp(value.created_at),
    )


def _write_values(value: CheckpointWrite) -> tuple[Any, ...]:
    return (
        value.task_id,
        value.channel,
        value.type_name,
        value.payload,
        value.task_path,
    )


async def _lock_active_run(connection: Any, run_id: str) -> None:
    """Lock and validate the run before mutating its checkpoints."""

    row = await connection.fetchrow(
        "SELECT status FROM harnest_runs WHERE run_id=$1 FOR UPDATE", run_id
    )
    if row is None:
        raise KeyError("checkpoint run not found")
    if row["status"] not in {"running", "waiting"}:
        raise CheckpointConflictError("checkpoint run is already terminal")


async def _validate_checkpoint_revision(
    connection: Any,
    checkpoint: CheckpointRecord,
    expected_revision: int | None,
) -> None:
    """Compare the stored revision under the caller's transaction lock."""

    row = await connection.fetchrow(
        """
        SELECT revision FROM harnest_checkpoints
        WHERE run_id=$1 AND namespace=$2 AND checkpoint_id=$3
        """,
        checkpoint.run_id,
        checkpoint.namespace,
        checkpoint.checkpoint_id,
    )
    actual = None if row is None else row["revision"]
    if actual != expected_revision:
        raise CheckpointConflictError("checkpoint revision changed")


def _pending_dump(value: PendingAction | None) -> str | None:
    if value is None:
        return None
    return json.dumps(
        {"type": value.type, "action_id": value.action_id, "capability": value.capability}
    )


def _json_dump(value: Any) -> str:
    return json.dumps(json_value(value), separators=(",", ":"), sort_keys=True)


def _json_load(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _parse_timestamp(value: str) -> datetime:
    """Decode portable ISO timestamps at the asyncpg type boundary."""

    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("checkpoint created_at must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("checkpoint created_at must include a timezone")
    return parsed


def _lock_key(user_id: str, session_id: str) -> int:
    """Derive a stable signed key without exposing tenant identity to PostgreSQL."""

    digest = hashlib.sha256(f"{user_id}\0{session_id}".encode()).digest()[:8]
    return int.from_bytes(digest, "big", signed=True)


def _require_text(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_limit(limit: int) -> None:
    if limit < 1:
        raise ValueError("checkpoint list limit must be positive")


def _audit(operation: str, trigger: str, outcome: str, backend: str) -> None:
    """Emit a payload-free signal for each attempted durable mutation."""

    # User IDs, session IDs, checkpoint bytes, and state remain out of telemetry.
    _AUDIT.info(
        operation,
        operation=operation,
        trigger=trigger,
        outcome=outcome,
        backend=backend,
    )


__all__ = ["PostgresStore"]
