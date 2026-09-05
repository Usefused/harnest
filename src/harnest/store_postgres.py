"""PostgreSQL-backed Harnest session, checkpoint, and continuation storage."""

from __future__ import annotations

import asyncio
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
    A2ATaskConflictError,
    A2ATaskCursorError,
    A2ATaskPage,
    A2ATaskRecord,
    CheckpointConflictError,
    CheckpointRecord,
    CheckpointWrite,
    HarnestStore,
    PendingAction,
    RunRecord,
    RunScope,
    RunStatus,
    _require_checkpoint_scope,
    _require_a2a_list_options,
    _require_fields,
    _validate_same_run,
    _validate_transition,
)
from .continuation import (
    ContinuationConflictError,
    ContinuationFailure,
    ContinuationRecord,
    ProviderPendingContinuation,
    _require_page,
    _validated_resolution,
    audit_continuation,
    external_id_key,
)
from .durable import ResumeArtifact
from .logging import get_logger
from .runtime_contract import SessionConflictError, SessionRecord
from .session import SessionLease, _require_list_options
from .store_postgres_schema import SCHEMA_LOCK, SCHEMA_SQL, SCHEMA_VERSION


_AUDIT = get_logger("store.audit")
_A2A_FILTER = """
application_id=$1 AND user_id=$2
AND ($3::text IS NULL OR context_id=$3)
AND ($4::smallint IS NULL OR status=$4)
AND ($5::timestamptz IS NULL OR status_timestamp >= $5)
"""
_A2A_COUNT_QUERY = f"SELECT count(*) FROM harnest_a2a_tasks WHERE {_A2A_FILTER}"
_A2A_CURSOR_QUERY = f"""
SELECT status_timestamp FROM harnest_a2a_tasks
WHERE {_A2A_FILTER} AND task_id=$6
"""
_A2A_LIST_QUERY = f"""
SELECT * FROM harnest_a2a_tasks
WHERE {_A2A_FILTER}
AND (
    $7::text IS NULL
    OR (
        $6::timestamptz IS NOT NULL
        AND (
            status_timestamp < $6
            OR status_timestamp IS NULL
            OR (status_timestamp=$6 AND task_id <= $7)
        )
    )
    OR (
        $6::timestamptz IS NULL
        AND status_timestamp IS NULL
        AND task_id <= $7
    )
)
ORDER BY status_timestamp DESC NULLS LAST, task_id DESC
LIMIT $8
"""


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
        lease_pool_options: Mapping[str, Any] | None = None,
        setup_schema: bool = True,
        _pool: Any = None,
        _lease_pool: Any = None,
    ) -> None:
        """Configure schema ownership and optional session-lease isolation."""

        _require_text(dsn, "dsn")
        self._dsn = dsn
        self._pool_options = dict(pool_options or {})
        self._lease_pool_options = (
            None if lease_pool_options is None else dict(lease_pool_options)
        )
        self._setup_schema = setup_schema
        self._pool = _pool
        self._owns_pool = _pool is None
        # Absence preserves the single-pool contract. Supplying options creates
        # a dedicated lane so long model executions cannot starve short DB work.
        self._lease_pool = (
            _pool
            if _lease_pool is None and lease_pool_options is None
            else _lease_pool
        )
        self._owns_lease_pool = (
            _lease_pool is None and lease_pool_options is not None
        )

    async def start(self) -> None:
        """Open the pool and create or validate the Harnest-owned schema."""

        if self._pool is None:
            self._pool = await _create_pool(self._dsn, self._pool_options)
        if self._lease_pool is None:
            self._lease_pool = (
                self._pool
                if self._lease_pool_options is None
                else await _create_pool(self._dsn, self._lease_pool_options)
            )
        if self._setup_schema:
            await self._bootstrap_schema()
        else:
            await self._validate_schema()

    def _task_database_dsn(self) -> str:
        """Share connection configuration without exposing it as agent context."""

        # Queue ownership opens an independent pool so Harnest and Procrastinate
        # cannot unexpectedly consume or close each other's connection leases.
        return self._dsn

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
        """Read one tenant-scoped session from PostgreSQL."""

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

    async def put_a2a_task(self, record: A2ATaskRecord) -> A2ATaskRecord:
        """Upsert an owner-scoped protobuf snapshot with immutable context."""

        query = """
        INSERT INTO harnest_a2a_tasks(
            application_id, user_id, task_id, context_id, status,
            status_timestamp, payload, created_at, updated_at
        ) VALUES ($1,$2,$3,$4,$5,$6::timestamptz,$7,$8::timestamptz,now())
        ON CONFLICT (application_id, user_id, task_id) DO UPDATE SET
            status=EXCLUDED.status,
            status_timestamp=EXCLUDED.status_timestamp,
            payload=EXCLUDED.payload,
            updated_at=now()
        WHERE harnest_a2a_tasks.context_id=EXCLUDED.context_id
        RETURNING *
        """
        arguments = _a2a_task_values(record)
        try:
            async with self._connection() as connection:
                row = await connection.fetchrow(query, *arguments)
        except Exception:
            _audit("a2a.task_saved", "agent", "failed", "postgres")
            raise
        if row is None:
            _audit("a2a.task_saved", "agent", "failed", "postgres")
            raise A2ATaskConflictError(
                "A2A task context cannot change after creation"
            )
        _audit("a2a.task_saved", "agent", "committed", "postgres")
        return _a2a_task_from_row(row)

    async def get_a2a_task(
        self, *, application_id: str, user_id: str, task_id: str
    ) -> A2ATaskRecord | None:
        """Read one A2A task using its complete application and owner scope."""

        _require_fields(application_id, user_id, task_id)
        async with self._connection() as connection:
            row = await connection.fetchrow(
                """
                SELECT * FROM harnest_a2a_tasks
                WHERE application_id=$1 AND user_id=$2 AND task_id=$3
                """,
                application_id,
                user_id,
                task_id,
            )
        return None if row is None else _a2a_task_from_row(row)

    async def list_a2a_tasks(
        self,
        *,
        application_id: str,
        user_id: str,
        context_id: str | None = None,
        status: int | None = None,
        status_timestamp_after: str | None = None,
        cursor_task_id: str | None = None,
        limit: int = 51,
    ) -> A2ATaskPage:
        """Apply A2A filters, ordering, and keyset pagination in PostgreSQL."""

        _require_a2a_list_options(
            application_id,
            user_id,
            context_id,
            status,
            status_timestamp_after,
            cursor_task_id,
            limit,
        )
        arguments = _a2a_list_values(
            application_id,
            user_id,
            context_id,
            status,
            status_timestamp_after,
        )
        async with self._connection() as connection:
            cursor_timestamp = await _postgres_a2a_cursor(
                connection, arguments, cursor_task_id
            )
            total = await connection.fetchval(_A2A_COUNT_QUERY, *arguments)
            rows = await connection.fetch(
                _A2A_LIST_QUERY,
                *arguments,
                cursor_timestamp,
                cursor_task_id,
                limit,
            )
        return A2ATaskPage(
            tuple(_a2a_task_from_row(row) for row in rows), int(total)
        )

    async def delete_a2a_task(
        self, *, application_id: str, user_id: str, task_id: str
    ) -> bool:
        """Delete one exact A2A owner record without revealing foreign tasks."""

        _require_fields(application_id, user_id, task_id)
        try:
            async with self._connection() as connection:
                row = await connection.fetchrow(
                    """
                    DELETE FROM harnest_a2a_tasks
                    WHERE application_id=$1 AND user_id=$2 AND task_id=$3
                    RETURNING task_id
                    """,
                    application_id,
                    user_id,
                    task_id,
                )
        except Exception:
            _audit("a2a.task_deleted", "user", "failed", "postgres")
            raise
        if row is not None:
            _audit("a2a.task_deleted", "user", "committed", "postgres")
        return row is not None

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
                        """
                        SELECT * FROM harnest_runs
                        WHERE run_id=$1 AND application_id=$2
                          AND user_id=$3 AND session_id=$4
                        """,
                        run_id,
                        application_id,
                        user_id,
                        session_id,
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

    async def get_run(self, *, scope: RunScope) -> RunRecord | None:
        """Read one PostgreSQL checkpoint run through its ownership scope."""

        async with self._connection() as connection:
            row = await connection.fetchrow(
                """
                SELECT * FROM harnest_runs
                WHERE run_id=$1 AND application_id=$2
                  AND user_id=$3 AND session_id=$4
                """,
                *_scope_values(scope),
            )
        return None if row is None else _run_from_row(row)

    async def get_checkpoint(
        self,
        *,
        scope: RunScope,
        checkpoint_id: str | None = None,
        namespace: str = "",
    ) -> CheckpointRecord | None:
        """Load an exact or newest checkpoint entirely in PostgreSQL."""

        if checkpoint_id is None:
            query = """
            SELECT checkpoint.* FROM harnest_checkpoints AS checkpoint
            JOIN harnest_runs AS run USING (run_id)
            WHERE checkpoint.run_id=$1 AND run.application_id=$2
              AND run.user_id=$3 AND run.session_id=$4
              AND checkpoint.namespace=$5
            ORDER BY checkpoint.revision DESC, checkpoint.checkpoint_id DESC
            LIMIT 1
            """
            arguments = (*_scope_values(scope), namespace)
        else:
            query = """
            SELECT checkpoint.* FROM harnest_checkpoints AS checkpoint
            JOIN harnest_runs AS run USING (run_id)
            WHERE checkpoint.run_id=$1 AND run.application_id=$2
              AND run.user_id=$3 AND run.session_id=$4
              AND checkpoint.namespace=$5 AND checkpoint.checkpoint_id=$6
            """
            arguments = (*_scope_values(scope), namespace, checkpoint_id)
        async with self._connection() as connection:
            row = await connection.fetchrow(query, *arguments)
        return None if row is None else _checkpoint_from_row(row)

    async def list_checkpoints(
        self,
        *,
        scope: RunScope,
        namespace: str | None = None,
        before: str | None = None,
        limit: int = 20,
    ) -> AsyncIterator[CheckpointRecord]:
        """Push checkpoint filtering, ordering, cursor, and limit into SQL."""

        _require_limit(limit)
        query = """
        SELECT checkpoint.* FROM harnest_checkpoints AS checkpoint
        JOIN harnest_runs AS run USING (run_id)
        WHERE checkpoint.run_id=$1 AND run.application_id=$2
          AND run.user_id=$3 AND run.session_id=$4
          AND ($5::text IS NULL OR checkpoint.namespace=$5)
          AND ($6::text IS NULL OR checkpoint.checkpoint_id < $6)
        ORDER BY checkpoint.revision DESC, checkpoint.checkpoint_id DESC
        LIMIT $7
        """
        async with self._connection() as connection:
            rows = await connection.fetch(
                query, *_scope_values(scope), namespace, before, limit
            )
        for row in rows:
            yield _checkpoint_from_row(row)

    async def put(
        self,
        checkpoint: CheckpointRecord,
        *,
        scope: RunScope,
        expected_revision: int | None,
    ) -> CheckpointRecord:
        """Allocate a serialized revision and write only on matching CAS state."""

        _require_checkpoint_scope(checkpoint, scope)
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
                    await _lock_active_run(connection, scope)
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
        scope: RunScope,
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
            $6::text[], $7::text[], $8::text[], $9::bytea[], $10::text[]
        ) AS item(task_id, channel, type_name, payload, task_path)
        WHERE EXISTS (
            SELECT 1 FROM harnest_runs
            WHERE run_id=$1 AND application_id=$3 AND user_id=$4 AND session_id=$5
              AND status IN ('running', 'waiting')
        )
        ON CONFLICT DO NOTHING
        """
        columns = tuple(zip(*(_write_values(item) for item in writes)))
        try:
            async with self._connection() as connection:
                status = await connection.execute(
                    query,
                    scope.run_id,
                    checkpoint_id,
                    scope.application_id,
                    scope.user_id,
                    scope.session_id,
                    *columns,
                )
            if status == "INSERT 0 0":
                await self._require_active_run(scope)
        except Exception:
            _audit("checkpoint.writes_saved", "agent", "failed", "postgres")
            raise
        _audit("checkpoint.writes_saved", "agent", "committed", "postgres")

    async def get_writes(
        self, *, scope: RunScope, checkpoint_id: str
    ) -> Sequence[CheckpointWrite]:
        """Read pending writes for one owned PostgreSQL checkpoint."""

        async with self._connection() as connection:
            rows = await connection.fetch(
                """
                SELECT task_id, channel, type_name, payload, task_path
                FROM harnest_checkpoint_writes AS write
                WHERE write.run_id=$1 AND write.checkpoint_id=$5
                  AND EXISTS (
                    SELECT 1 FROM harnest_runs AS run
                    WHERE run.run_id=write.run_id AND run.application_id=$2
                      AND run.user_id=$3 AND run.session_id=$4
                  )
                ORDER BY task_id, channel
                """,
                *_scope_values(scope),
                checkpoint_id,
            )
        return tuple(_write_from_row(row) for row in rows)

    async def get_writes_batch(
        self, *, scope: RunScope, checkpoint_ids: Sequence[str]
    ) -> Mapping[str, Sequence[CheckpointWrite]]:
        """Fetch pending writes for several checkpoints in one query."""

        if not checkpoint_ids:
            return {}
        async with self._connection() as connection:
            rows = await connection.fetch(
                """
                SELECT checkpoint_id, task_id, channel, type_name, payload, task_path
                FROM harnest_checkpoint_writes AS write
                WHERE write.run_id=$1 AND write.checkpoint_id=ANY($5::text[])
                  AND EXISTS (
                    SELECT 1 FROM harnest_runs AS run
                    WHERE run.run_id=write.run_id AND run.application_id=$2
                      AND run.user_id=$3 AND run.session_id=$4
                  )
                ORDER BY checkpoint_id, task_id, channel
                """,
                *_scope_values(scope),
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

    async def suspend_continuation(
        self, *, record: ContinuationRecord, external_id: str
    ) -> ContinuationRecord:
        """Atomically persist a provider wait and mark its owned run waiting."""

        query = """
        WITH inserted AS (
            INSERT INTO harnest_continuations(
                continuation_id, run_id, application_id, user_id, session_id,
                provider, capability, schema_id, resume, external_id, external_key,
                status, revision, ready, created_at, updated_at
            )
            SELECT $1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11,'pending',0,false,
                   $12::timestamptz,$13::timestamptz
            FROM harnest_runs
            WHERE run_id=$2 AND application_id=$3 AND user_id=$4
              AND session_id=$5 AND status='running'
            ON CONFLICT DO NOTHING
            RETURNING *
        ), waiting AS (
            UPDATE harnest_runs AS run SET
                status='waiting', pending_action=$14::jsonb,
                revision=run.revision + 1, updated_at=now()
            FROM inserted
            WHERE run.run_id=inserted.run_id AND run.status='running'
            RETURNING run.run_id
        )
        SELECT inserted.* FROM inserted JOIN waiting USING (run_id)
        """
        arguments = _continuation_insert_values(record, external_id)
        try:
            async with self._connection() as connection:
                async with connection.transaction():
                    row = await connection.fetchrow(query, *arguments)
        except Exception:
            audit_continuation("suspended", "failed", "postgres")
            raise
        if row is None:
            audit_continuation("suspended", "failed", "postgres")
            raise ContinuationConflictError(
                "continuation already exists or run is not running"
            )
        audit_continuation("suspended", "committed", "postgres")
        return _continuation_from_row(row)

    async def get_continuation(
        self, *, scope: RunScope, continuation_id: str
    ) -> ContinuationRecord | None:
        """Load one continuation with its complete ownership predicate in SQL."""

        async with self._connection() as connection:
            row = await connection.fetchrow(
                """
                SELECT * FROM harnest_continuations
                WHERE continuation_id=$1 AND run_id=$2 AND application_id=$3
                  AND user_id=$4 AND session_id=$5
                """,
                continuation_id,
                *_scope_values(scope),
            )
        return None if row is None else _continuation_from_row(row)

    async def get_provider_continuation(
        self, *, scope: RunScope, provider: str, continuation_id: str
    ) -> ProviderPendingContinuation | None:
        """Load one exact provider wait and its private external identity."""

        _require_fields(provider, continuation_id)
        async with self._connection() as connection:
            row = await connection.fetchrow(
                """
                SELECT * FROM harnest_continuations
                WHERE continuation_id=$1 AND run_id=$2 AND application_id=$3
                  AND user_id=$4 AND session_id=$5 AND provider=$6
                """,
                continuation_id,
                *_scope_values(scope),
                provider,
            )
        if row is None:
            return None
        return ProviderPendingContinuation(
            _continuation_from_row(row), row["external_id"]
        )

    async def get_continuation_by_external_id(
        self, *, application_id: str, provider: str, external_id: str
    ) -> ProviderPendingContinuation | None:
        """Use the unique provider key so replica callbacks never scan waits."""

        _require_fields(application_id, provider, external_id)
        async with self._connection() as connection:
            row = await connection.fetchrow(
                """
                SELECT * FROM harnest_continuations
                WHERE application_id=$1 AND provider=$2 AND external_key=$3
                """,
                application_id,
                provider,
                external_id_key(provider, external_id),
            )
        if row is None:
            return None
        return ProviderPendingContinuation(_continuation_from_row(row), row["external_id"])

    async def list_pending_continuations(
        self,
        *,
        application_id: str,
        provider: str,
        after: str | None = None,
        limit: int = 100,
    ) -> Sequence[ProviderPendingContinuation]:
        """Push provider reconciliation filtering and pagination into PostgreSQL."""

        _require_fields(application_id, provider)
        _require_page(after, limit)
        async with self._connection() as connection:
            rows = await connection.fetch(
                """
                SELECT * FROM harnest_continuations
                WHERE application_id=$1 AND provider=$2 AND status='pending'
                  AND ($3::text IS NULL OR continuation_id > $3)
                ORDER BY continuation_id
                LIMIT $4
                """,
                application_id,
                provider,
                after,
                limit,
            )
        return tuple(
            ProviderPendingContinuation(
                _continuation_from_row(row), row["external_id"]
            )
            for row in rows
        )

    async def resolve_continuation(
        self,
        *,
        scope: RunScope,
        provider: str,
        external_id: str,
        schema_id: str,
        result: Any = None,
        failure: ContinuationFailure | None = None,
    ) -> ContinuationRecord:
        """Commit one validated provider outcome with an indexed SQL CAS."""

        result, failure = _validated_resolution(result, failure)
        status = "failed" if failure is not None else "completed"
        query = """
        UPDATE harnest_continuations SET
            status=$8, result=$9::jsonb, failure=$10::jsonb,
            revision=revision + 1, updated_at=now()
        WHERE run_id=$1 AND application_id=$2 AND user_id=$3 AND session_id=$4
          AND provider=$5 AND external_key=$6 AND schema_id=$7
          AND status='pending'
        RETURNING *
        """
        arguments = (
            *_scope_values(scope),
            provider,
            external_id_key(provider, external_id),
            schema_id,
            status,
            _json_dump(result),
            _failure_dump(failure),
        )
        try:
            async with self._connection() as connection:
                row = await connection.fetchrow(query, *arguments)
        except Exception:
            audit_continuation("resolved", "failed", "postgres")
            raise
        if row is None:
            audit_continuation("resolved", "failed", "postgres")
            raise ContinuationConflictError("continuation state changed")
        audit_continuation("resolved", "committed", "postgres")
        return _continuation_from_row(row)

    async def claim_continuation(
        self,
        *,
        scope: RunScope,
        provider: str,
        continuation_id: str,
        expected_revision: int,
    ) -> ContinuationRecord:
        """Claim an outcome and resume its run in one PostgreSQL statement."""

        query = """
        WITH target AS (
            SELECT continuation.* FROM harnest_continuations AS continuation
            JOIN harnest_runs AS run USING (run_id)
            WHERE continuation.continuation_id=$1
              AND continuation.run_id=$2 AND continuation.application_id=$3
              AND continuation.user_id=$4 AND continuation.session_id=$5
              AND continuation.provider=$6 AND continuation.revision=$7
              AND continuation.status IN ('completed', 'failed')
              AND continuation.ready=true
              AND run.status='waiting'
              AND run.pending_action->>'action_id'=$1
            FOR UPDATE OF continuation, run
        ), resumed AS (
            UPDATE harnest_runs AS run SET
                status='running', pending_action=NULL,
                revision=run.revision + 1, updated_at=now()
            FROM target WHERE run.run_id=target.run_id
            RETURNING run.run_id
        )
        UPDATE harnest_continuations AS continuation SET
            status='claimed', revision=continuation.revision + 1,
            updated_at=now()
        FROM target JOIN resumed USING (run_id)
        WHERE continuation.continuation_id=target.continuation_id
        RETURNING continuation.*
        """
        arguments = (
            continuation_id,
            *_scope_values(scope),
            provider,
            expected_revision,
        )
        try:
            async with self._connection() as connection:
                row = await connection.fetchrow(query, *arguments)
        except Exception:
            audit_continuation("claimed", "failed", "postgres")
            raise
        if row is None:
            audit_continuation("claimed", "failed", "postgres")
            raise ContinuationConflictError("continuation claim changed")
        audit_continuation("claimed", "committed", "postgres")
        return _continuation_from_row(row)

    async def cancel_continuation(
        self,
        *,
        scope: RunScope,
        provider: str,
        continuation_id: str,
        expected_revision: int,
        failure: ContinuationFailure,
    ) -> ContinuationRecord:
        """Fail a pending continuation and cancel its waiting run atomically."""

        if not isinstance(failure, ContinuationFailure):
            audit_continuation("cancelled", "failed", "postgres")
            raise TypeError("continuation cancellation failure is invalid")
        query = """
        WITH target AS (
            SELECT continuation.* FROM harnest_continuations AS continuation
            JOIN harnest_runs AS run USING (run_id)
            WHERE continuation.continuation_id=$1
              AND continuation.run_id=$2 AND continuation.application_id=$3
              AND continuation.user_id=$4 AND continuation.session_id=$5
              AND continuation.provider=$6 AND continuation.revision=$7
              AND continuation.status='pending' AND run.status='waiting'
              AND run.application_id=$3 AND run.user_id=$4
              AND run.session_id=$5
              AND run.pending_action->>'action_id'=$1
            FOR UPDATE OF continuation, run
        ), cancelled AS (
            UPDATE harnest_runs AS run SET
                status='cancelled', pending_action=NULL,
                revision=run.revision + 1, updated_at=now()
            FROM target WHERE run.run_id=target.run_id
            RETURNING run.run_id
        )
        UPDATE harnest_continuations AS continuation SET
            status='failed', failure=$8::jsonb,
            revision=continuation.revision + 1, updated_at=now()
        FROM target JOIN cancelled USING (run_id)
        WHERE continuation.continuation_id=target.continuation_id
        RETURNING continuation.*
        """
        arguments = (
            continuation_id,
            *_scope_values(scope),
            provider,
            expected_revision,
            _failure_dump(failure),
        )
        try:
            async with self._connection() as connection:
                row = await connection.fetchrow(query, *arguments)
        except Exception:
            audit_continuation("cancelled", "failed", "postgres")
            raise
        if row is None:
            audit_continuation("cancelled", "failed", "postgres")
            raise ContinuationConflictError("continuation cancellation changed")
        audit_continuation("cancelled", "committed", "postgres")
        return _continuation_from_row(row)

    async def arm_continuation(
        self,
        *,
        scope: RunScope,
        provider: str,
        continuation_id: str,
        expected_revision: int,
    ) -> ContinuationRecord:
        """Arm one wait with a SQL CAS after native checkpoint persistence."""

        query = """
        UPDATE harnest_continuations SET
            ready=true, revision=revision + 1, updated_at=now()
        WHERE continuation_id=$1 AND run_id=$2 AND application_id=$3
          AND user_id=$4 AND session_id=$5 AND provider=$6
          AND revision=$7 AND ready=false
          AND status IN ('pending', 'completed', 'failed')
        RETURNING *
        """
        try:
            async with self._connection() as connection:
                row = await connection.fetchrow(
                    query,
                    continuation_id,
                    *_scope_values(scope),
                    provider,
                    expected_revision,
                )
        except Exception:
            audit_continuation("armed", "failed", "postgres")
            raise
        if row is None:
            audit_continuation("armed", "failed", "postgres")
            raise ContinuationConflictError("continuation arm changed")
        audit_continuation("armed", "committed", "postgres")
        return _continuation_from_row(row)

    async def transition(
        self,
        *,
        scope: RunScope,
        expected_status: RunStatus,
        status: RunStatus,
        pending_action: PendingAction | None = None,
    ) -> RunRecord:
        """Apply a validated status transition with one SQL compare-and-swap."""

        _validate_transition(expected_status, status, pending_action)
        query = """
        UPDATE harnest_runs SET
            status=$6, pending_action=$7::jsonb,
            revision=revision + 1, updated_at=now()
        WHERE run_id=$1 AND application_id=$2 AND user_id=$3 AND session_id=$4
          AND status=$5 RETURNING *
        """
        try:
            async with self._connection() as connection:
                row = await connection.fetchrow(
                    query,
                    *_scope_values(scope),
                    expected_status,
                    status,
                    _pending_dump(pending_action),
                )
        except Exception:
            _audit("checkpoint.transition", "agent", "failed", "postgres")
            raise
        if row is None:
            try:
                await self._raise_transition_conflict(scope)
            except Exception:
                _audit("checkpoint.transition", "agent", "failed", "postgres")
                raise
        record = _run_from_row(row)
        _audit("checkpoint.transition", "agent", "committed", "postgres")
        return record

    async def delete_run(self, *, scope: RunScope) -> None:
        """Delete a run and cascade its private checkpoint data."""

        try:
            async with self._connection() as connection:
                row = await connection.fetchrow(
                    """
                    DELETE FROM harnest_runs
                    WHERE run_id=$1 AND application_id=$2
                      AND user_id=$3 AND session_id=$4
                    RETURNING *
                    """,
                    *_scope_values(scope),
                )
        except Exception:
            _audit("checkpoint.run_deleted", "user", "failed", "postgres")
            raise
        if row is not None:
            _audit("checkpoint.run_deleted", "user", "committed", "postgres")

    async def close(self) -> None:
        """Close only pools created and owned by this store."""

        lease_pool, self._lease_pool = self._lease_pool, None
        pool, self._pool = self._pool, None
        try:
            if (
                lease_pool is not None
                and lease_pool is not pool
                and self._owns_lease_pool
            ):
                await lease_pool.close()
        finally:
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

    async def _require_active_run(self, scope: RunScope) -> RunRecord:
        """Explain a zero-row set write as missing or terminal run state."""

        record = await self.get_run(scope=scope)
        if record is None:
            raise KeyError("checkpoint run not found")
        if record.status not in {"running", "waiting"}:
            raise CheckpointConflictError("checkpoint run is already terminal")
        return record

    async def _raise_transition_conflict(self, scope: RunScope) -> None:
        """Distinguish a missing run from a stale status transition."""

        if await self.get_run(scope=scope) is None:
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
    async def _lease_connection(self) -> AsyncIterator[Any]:
        """Acquire the lane reserved for long-lived session execution leases."""

        if self._lease_pool is None:
            raise RuntimeError("PostgresStore.start() must be called first")
        async with self._lease_pool.acquire() as connection:
            yield connection

    @asynccontextmanager
    async def _locked_session_connection(
        self, user_id: str, session_id: str
    ) -> AsyncIterator[Any]:
        """Keep the advisory lock and its owning connection at one boundary."""

        key = _lock_key(user_id, session_id)
        async with self._lease_connection() as connection:
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
        # ADK event persistence and application session data can commit from
        # sibling tasks. asyncpg permits only one in-flight command per connection.
        self._operation_lock = asyncio.Lock()

    @property
    def record(self) -> SessionRecord:
        return self._record

    async def patch_state(self, delta: Mapping[str, Any]) -> SessionRecord:
        normalized = json_value(delta)
        async with self._operation_lock:
            # Derive under the same gate as the write so concurrent patches do
            # not calculate replacements from the same stale record.
            state = {**dict(self._record.state), **normalized}
            return await self._replace_lane_locked("state", state)

    async def replace_state(self, state: Mapping[str, Any]) -> SessionRecord:
        """Replace leased state on the already locked connection."""

        return await self._replace_lane("state", state)

    async def replace_application_data(
        self, data: Mapping[str, Any]
    ) -> SessionRecord:
        """Commit app data independently from framework-owned session state."""

        return await self._replace_lane("application_data", data)

    async def _replace_lane(
        self, column: str, value: Mapping[str, Any]
    ) -> SessionRecord:
        """Serialize one replacement on the lease-owned asyncpg connection."""

        async with self._operation_lock:
            return await self._replace_lane_locked(column, value)

    async def _replace_lane_locked(
        self, column: str, value: Mapping[str, Any]
    ) -> SessionRecord:
        """Write one lane while the caller owns the per-connection gate."""

        # Column selection is internal and closed over known literals; values
        # remain query parameters so application data cannot affect SQL shape.
        if column not in {"state", "application_data"}:
            raise ValueError("unknown session data lane")
        query = f"""
        UPDATE harnest_sessions SET {column}=$3::jsonb, updated_at=now()
        WHERE user_id=$1 AND session_id=$2 RETURNING *
        """
        try:
            row = await self._connection.fetchrow(
                query,
                self._record.user_id,
                self._record.id,
                _json_dump(value),
            )
        except Exception:
            _audit("session.lease_update", "agent", "failed", "postgres")
            raise
        if row is None:
            _audit("session.lease_update", "agent", "failed", "postgres")
            raise KeyError("session not found")
        self._record = _session_from_row(row)
        _audit("session.lease_update", "agent", "committed", "postgres")
        return self._record


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
        application_data=_json_load(row.get("application_data", {})),
        created_at=_iso(row["created_at"]),
        updated_at=_iso(row["updated_at"]),
    )


def _a2a_task_from_row(row: Mapping[str, Any]) -> A2ATaskRecord:
    """Restore an opaque A2A task and its datastore query projections."""

    timestamp = row.get("status_timestamp")
    return A2ATaskRecord(
        application_id=row["application_id"],
        user_id=row["user_id"],
        task_id=row["task_id"],
        context_id=row["context_id"],
        status=int(row["status"]),
        status_timestamp=None if timestamp is None else _iso(timestamp),
        payload=bytes(row["payload"]),
        created_at=_iso(row["created_at"]),
        updated_at=_iso(row["updated_at"]),
    )


def _a2a_task_values(value: A2ATaskRecord) -> tuple[Any, ...]:
    """Preserve the positional contract for the durable task upsert."""

    return (
        value.application_id,
        value.user_id,
        value.task_id,
        value.context_id,
        value.status,
        _parse_optional_postgres_timestamp(value.status_timestamp),
        value.payload,
        _parse_timestamp(value.created_at),
    )


def _a2a_list_values(
    application_id: str,
    user_id: str,
    context_id: str | None,
    status: int | None,
    status_timestamp_after: str | None,
) -> tuple[Any, ...]:
    """Convert only timestamp data before binding the shared list predicates."""

    return (
        application_id,
        user_id,
        context_id,
        status,
        _parse_optional_postgres_timestamp(status_timestamp_after),
    )


async def _postgres_a2a_cursor(
    connection: Any,
    arguments: tuple[Any, ...],
    cursor_task_id: str | None,
) -> datetime | None:
    """Resolve and validate an inclusive cursor within the selected task set."""

    if cursor_task_id is None:
        return None
    row = await connection.fetchrow(
        _A2A_CURSOR_QUERY, *arguments, cursor_task_id
    )
    if row is None:
        raise A2ATaskCursorError("A2A task cursor is not valid for this list")
    return row["status_timestamp"]


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


def _continuation_from_row(row: Mapping[str, Any]) -> ContinuationRecord:
    """Restore private outcome lanes without returning the external-id mapping."""

    failure = _json_load(row.get("failure"))
    resume = _json_load(row.get("resume"))
    return ContinuationRecord(
        continuation_id=row["continuation_id"],
        application_id=row["application_id"],
        user_id=row["user_id"],
        session_id=row["session_id"],
        run_id=row["run_id"],
        provider=row["provider"],
        capability=row["capability"],
        schema_id=row["schema_id"],
        resume=None if resume is None else ResumeArtifact.from_mapping(resume),
        status=row["status"],
        ready=bool(row.get("ready", False)),
        revision=row["revision"],
        result=_json_load(row.get("result")),
        failure=None if failure is None else ContinuationFailure(**failure),
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


def _continuation_insert_values(
    value: ContinuationRecord, external_id: str
) -> tuple[Any, ...]:
    """Preserve the positional contract for the atomic suspend statement."""

    return (
        value.continuation_id,
        value.run_id,
        value.application_id,
        value.user_id,
        value.session_id,
        value.provider,
        value.capability,
        value.schema_id,
        _json_dump(
            None if value.resume is None else value.resume.public_dict()
        ),
        external_id,
        external_id_key(value.provider, external_id),
        _parse_timestamp(value.created_at),
        _parse_timestamp(value.updated_at),
        _pending_dump(value.pending_action),
    )


def _failure_dump(value: ContinuationFailure | None) -> str | None:
    """Serialize only the bounded failure category allowed by the domain model."""

    if value is None:
        return None
    return _json_dump({"code": value.code, "retryable": value.retryable})


async def _lock_active_run(connection: Any, scope: RunScope) -> None:
    """Lock and validate the run before mutating its checkpoints."""

    row = await connection.fetchrow(
        """
        SELECT status FROM harnest_runs
        WHERE run_id=$1 AND application_id=$2 AND user_id=$3 AND session_id=$4
        FOR UPDATE
        """,
        *_scope_values(scope),
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


def _scope_values(scope: RunScope) -> tuple[str, str, str, str]:
    return (
        scope.run_id,
        scope.application_id,
        scope.user_id,
        scope.session_id,
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
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("checkpoint created_at must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("checkpoint created_at must include a timezone")
    return parsed


def _parse_optional_postgres_timestamp(value: str | None) -> datetime | None:
    """Keep nullable A2A status timestamps distinct from creation timestamps."""

    return None if value is None else _parse_timestamp(value)


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
