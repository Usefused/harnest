"""PostgreSQL implementation of portable task and cron persistence contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import math
from typing import Any
import uuid

from .cron_storage import CronRecord, CronStore, CronStoreConflictError, cron_fingerprint
from .logging import get_logger
from .store_postgres import _create_pool
from .task_storage import TaskRecord, TaskStore, TaskStoreConflictError, task_fingerprint
from .task_store_postgres_schema import CLAIM_SQL, ENQUEUE_SQL, EXPIRE_SQL, FINISH_SQL, SCHEMA_SQL


_AUDIT = get_logger("store.audit")
_SCHEMA_LOCK = 489_867_841_435_466_308


class PostgresTaskStore(TaskStore, CronStore):
    """Persist jobs and schedules with transactional claims and fenced leases.

    ``_pool`` supports composing this provider with the combined session store;
    externally supplied pools remain owned by their caller.
    """

    def __init__(
        self, dsn: str, *, pool_options: Mapping[str, Any] | None = None,
        setup_schema: bool = True, _pool: Any = None,
    ) -> None:
        """Retain connection settings without importing the optional driver."""

        self._dsn = dsn
        self._pool_options = dict(pool_options or {})
        self._setup_schema = setup_schema
        self._pool = _pool
        self._owns_pool = _pool is None

    async def start(self) -> None:
        """Open one pool and install or validate the durable schema."""

        if self._pool is None:
            self._pool = await _create_pool(self._dsn, self._pool_options)
        await self._start_task_storage()

    async def _start_task_storage(self) -> None:
        """Share existing connections while serializing schema provisioning."""

        async with self._connection() as connection:
            async with connection.transaction():
                if self._setup_schema:
                    await connection.execute("SELECT pg_advisory_xact_lock($1)", _SCHEMA_LOCK)
                    await connection.execute(SCHEMA_SQL)
                else:
                    # An explicit projection detects incomplete provisioned schemas
                    # without silently creating database objects in restricted mode.
                    await connection.fetch("SELECT fingerprint FROM harnest_durable_tasks LIMIT 0")
                    await connection.fetch("SELECT revision FROM harnest_durable_crons LIMIT 0")

    async def close(self) -> None:
        """Close only the pool opened by this instance."""

        pool, self._pool = self._pool, None
        if pool is not None and self._owns_pool:
            await pool.close()

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Any]:
        """Reject unstarted use before acquiring a pooled connection."""

        if self._pool is None:
            raise RuntimeError("PostgresTaskStore.start() must be called first")
        async with self._pool.acquire() as connection:
            yield connection

    async def enqueue_task(self, record: TaskRecord) -> TaskRecord:
        """Persist an immutable task identity, returning exact idempotent retries."""

        async with _mutation("task.enqueue", record.trigger):
            async with self._connection() as connection:
                async with connection.transaction():
                    return await _enqueue(connection, record)

    async def get_task(
        self, *, application_id: str, job_id: str, user_id: str | None = None,
    ) -> TaskRecord | None:
        """Read a scoped job; omitted user scope is reserved for trusted workers."""

        async with self._connection() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM harnest_durable_tasks WHERE application_id=$1 "
                "AND job_id=$2 AND ($3::text IS NULL OR user_id=$3)",
                application_id, job_id, user_id,
            )
        return None if row is None else _task(row)

    async def claim_tasks(
        self, *, application_id: str, queues: tuple[str, ...], now: float,
        lease_seconds: float, limit: int = 1,
    ) -> tuple[TaskRecord, ...]:
        """Claim disjoint batches and retire exhausted attempts after crashes."""

        _require_claim_options(now, lease_seconds, limit)
        if not queues:
            return ()
        async with _mutation("task.claim", "worker"):
            async with self._connection() as connection:
                async with connection.transaction():
                    # Bound crash cleanup separately from runnable claims, and
                    # skip rows another replica is already retiring.
                    await connection.execute(EXPIRE_SQL, application_id, list(queues), now, limit)
                    rows = await connection.fetch(
                        CLAIM_SQL, application_id, list(queues), now, limit,
                        uuid.uuid4().hex, lease_seconds,
                    )
        return tuple(_task(row) for row in rows)

    async def renew_task_lease(
        self, *, application_id: str, job_id: str, lease_token: str,
        now: float, lease_seconds: float,
    ) -> bool:
        """Renew only an unexpired attempt still owned by the calling worker."""

        _require_claim_options(now, lease_seconds, 1)
        async with _mutation("task.renew", "worker"):
            async with self._connection() as connection:
                row = await connection.fetchrow(
                    "UPDATE harnest_durable_tasks SET lease_expires_at=$4+$5,updated_at=$4 "
                    "WHERE application_id=$1 AND job_id=$2 AND lease_token=$3 "
                    "AND status='running' AND lease_expires_at>$4 RETURNING job_id",
                    application_id, job_id, lease_token, now, lease_seconds,
                )
        return row is not None

    async def finish_task(
        self, *, application_id: str, job_id: str, lease_token: str, now: float,
        status: str, result: Any = None, failure_code: str | None = None,
        retry_at: float | None = None,
    ) -> bool:
        """Fence completion and retries; terminal transitions erase private input."""

        _require_finish_options(status, now, retry_at)
        async with _mutation("task.finish", "worker"):
            async with self._connection() as connection:
                row = await connection.fetchrow(
                    FINISH_SQL, application_id, job_id, lease_token, now, status,
                    _dump_optional(result), failure_code, retry_at,
                )
        return row is not None

    async def cancel_task(
        self, *, application_id: str, user_id: str, job_id: str, now: float,
    ) -> bool:
        """Cancel an owned nonterminal job and revoke any running attempt."""

        async with _mutation("task.cancel", "user"):
            async with self._connection() as connection:
                row = await connection.fetchrow(
                    "UPDATE harnest_durable_tasks SET status='cancelled',updated_at=$4,"
                    "arguments='{}'::jsonb,invocation=NULL,agent_permissions=NULL,"
                    "lease_token=NULL,lease_expires_at=NULL,result=NULL,failure_code=NULL "
                    "WHERE application_id=$1 AND user_id=$2 AND job_id=$3 "
                    "AND status IN ('pending','running') RETURNING job_id",
                    application_id, user_id, job_id, now,
                )
        return row is not None

    async def create_cron(self, record: CronRecord) -> CronRecord:
        """Create a schedule once without reactivating an existing cancelled key."""

        async with _mutation("cron.create", "user"):
            async with self._connection() as connection:
                async with connection.transaction():
                    row = await connection.fetchrow(
                        "INSERT INTO harnest_durable_crons(application_id,schedule_id,user_id,"
                        "schedule_key,expression,timezone,task_name,arguments,next_run_at,status,"
                        "revision,created_at,updated_at) "
                        "VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,$12,$13) "
                        "ON CONFLICT DO NOTHING RETURNING *", *_cron_values(record),
                    )
                    if row is not None:
                        return _cron(row)
                    return await _existing_cron(connection, record)

    async def get_cron(
        self, *, application_id: str, user_id: str, schedule_id: str,
    ) -> CronRecord | None:
        """Read a schedule only within its application and owner scope."""

        async with self._connection() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM harnest_durable_crons WHERE application_id=$1 "
                "AND user_id=$2 AND schedule_id=$3", application_id, user_id, schedule_id,
            )
        return None if row is None else _cron(row)

    async def list_crons(
        self, *, application_id: str, user_id: str, after: str | None = None,
        limit: int = 100,
    ) -> tuple[CronRecord, ...]:
        """Apply owner filtering and keyset pagination in the database."""

        _require_limit(limit)
        async with self._connection() as connection:
            rows = await connection.fetch(
                "SELECT * FROM harnest_durable_crons WHERE application_id=$1 AND user_id=$2 "
                "AND ($3::text IS NULL OR schedule_id>$3) ORDER BY schedule_id LIMIT $4",
                application_id, user_id, after, limit,
            )
        return tuple(_cron(row) for row in rows)

    async def update_cron(self, record: CronRecord, *, expected_revision: int) -> CronRecord:
        """Replace one schedule conditionally, preserving its immutable identity."""

        async with _mutation("cron.update", "user"):
            async with self._connection() as connection:
                async with connection.transaction():
                    row = await connection.fetchrow(
                        "SELECT * FROM harnest_durable_crons WHERE application_id=$1 "
                        "AND user_id=$2 AND schedule_id=$3 FOR UPDATE",
                        record.application_id, record.user_id, record.schedule_id,
                    )
                    updated = _cron_update(row, record, expected_revision)
                    await connection.execute(
                        "UPDATE harnest_durable_crons SET expression=$4,arguments=$5::jsonb,"
                        "next_run_at=$6,status=$7,revision=$8,updated_at=$9 "
                        "WHERE application_id=$1 AND user_id=$2 AND schedule_id=$3",
                        updated.application_id, updated.user_id, updated.schedule_id,
                        updated.expression, _dump(updated.arguments), updated.next_run_at,
                        updated.status, updated.revision, updated.updated_at,
                    )
        return updated

    async def delete_cron(
        self, *, application_id: str, user_id: str, schedule_id: str,
    ) -> bool:
        """Delete a schedule using the same owner predicate as reads."""

        async with _mutation("cron.delete", "user"):
            async with self._connection() as connection:
                row = await connection.fetchrow(
                    "DELETE FROM harnest_durable_crons WHERE application_id=$1 "
                    "AND user_id=$2 AND schedule_id=$3 RETURNING schedule_id",
                    application_id, user_id, schedule_id,
                )
        return row is not None

    async def list_due_crons(
        self, *, application_id: str, now: float,
        after: tuple[float, str] | None = None, limit: int = 100,
    ) -> tuple[CronRecord, ...]:
        """Page due schedules in database order without loading inactive rows."""

        _require_limit(limit)
        cursor_time, cursor_id = (None, None) if after is None else after
        async with self._connection() as connection:
            rows = await connection.fetch(
                "SELECT * FROM harnest_durable_crons WHERE application_id=$1 "
                "AND status='active' AND next_run_at<=$2 "
                "AND ($3::double precision IS NULL OR (next_run_at,schedule_id)>($3,$4)) "
                "ORDER BY next_run_at,schedule_id LIMIT $5",
                application_id, now, cursor_time, cursor_id, limit,
            )
        return tuple(_cron(row) for row in rows)

    async def commit_cron_occurrence(
        self, *, application_id: str, user_id: str, schedule_id: str,
        expected_revision: int, due_at: float, next_run_at: float, task: TaskRecord,
    ) -> TaskRecord | None:
        """Atomically enqueue an occurrence and advance its schedule revision."""

        _require_occurrence(application_id, user_id, due_at, next_run_at, task)
        async with _mutation("cron.dispatch", "cron"):
            async with self._connection() as connection:
                async with connection.transaction():
                    row = await connection.fetchrow(
                        "UPDATE harnest_durable_crons SET next_run_at=$6,revision=revision+1,"
                        "updated_at=$7 WHERE application_id=$1 AND user_id=$2 AND schedule_id=$3 "
                        "AND revision=$4 AND next_run_at=$5 AND status='active' RETURNING *",
                        application_id, user_id, schedule_id, expected_revision, due_at,
                        next_run_at, task.created_at,
                    )
                    if row is None:
                        return None
                    if row["task_name"] != task.task_name or _decode(row["arguments"]) != dict(task.arguments):
                        raise CronStoreConflictError("occurrence does not match its schedule")
                    # Any enqueue conflict aborts this transaction, restoring the
                    # original due time so durable work is never silently skipped.
                    return await _enqueue(connection, task)


async def _enqueue(connection: Any, record: TaskRecord) -> TaskRecord:
    """Resolve both job-ID and per-task idempotency conflicts under a transaction."""

    fingerprint = task_fingerprint(record)
    row = await connection.fetchrow(ENQUEUE_SQL, *_task_values(record, fingerprint))
    if row is not None:
        return _task(row)
    rows = await connection.fetch(
        "SELECT * FROM harnest_durable_tasks WHERE application_id=$1 AND "
        "(job_id=$2 OR (user_id=$3 AND task_name=$4 AND idempotency_key=$5)) LIMIT 2",
        record.application_id, record.job_id, record.user_id, record.task_name,
        record.idempotency_key,
    )
    if len(rows) != 1:
        raise TaskStoreConflictError("task identity conflicts with existing work")
    row = rows[0]
    if row["user_id"] != record.user_id or row["fingerprint"] != fingerprint:
        raise TaskStoreConflictError("task identity conflicts with existing work")
    return _task(row)


async def _existing_cron(connection: Any, record: CronRecord) -> CronRecord:
    """Accept exact retries while refusing collisions across owner identities."""

    rows = await connection.fetch(
        "SELECT * FROM harnest_durable_crons WHERE application_id=$1 AND "
        "(schedule_id=$2 OR (user_id=$3 AND schedule_key=$4)) LIMIT 2",
        record.application_id, record.schedule_id, record.user_id, record.key,
    )
    if len(rows) != 1:
        raise CronStoreConflictError("schedule identity conflicts with existing work")
    existing = _cron(rows[0])
    if existing.user_id != record.user_id or cron_fingerprint(existing) != cron_fingerprint(record):
        raise CronStoreConflictError("schedule identity conflicts with existing work")
    return existing


def _task_values(record: TaskRecord, fingerprint: str) -> tuple[Any, ...]:
    """Encode private JSON only at the driver boundary."""

    return (
        record.application_id, record.job_id, record.user_id, record.task_name,
        record.queue, _dump(record.arguments), _dump_optional(record.invocation),
        _dump_optional(record.agent_permissions), record.trigger, record.status,
        record.scheduled_at, record.max_retries, record.attempt, record.lease_token,
        record.lease_expires_at, _dump_optional(record.result), record.failure_code,
        record.idempotency_key, fingerprint, record.created_at, record.updated_at,
    )


def _cron_values(record: CronRecord) -> tuple[Any, ...]:
    """Keep the persisted schedule shape independent of runtime classes."""

    return (
        record.application_id, record.schedule_id, record.user_id, record.key,
        record.expression, record.timezone, record.task_name, _dump(record.arguments),
        record.next_run_at, record.status, record.revision, record.created_at, record.updated_at,
    )


def _task(row: Mapping[str, Any]) -> TaskRecord:
    """Decode a projected SQL row without exposing internal fingerprints."""

    values = dict(row)
    values.pop("fingerprint", None)
    for field in ("arguments", "invocation", "agent_permissions", "result"):
        values[field] = _decode(values[field])
    if values["agent_permissions"] is not None:
        values["agent_permissions"] = tuple(values["agent_permissions"])
    return TaskRecord(**values)


def _cron_update(
    row: Mapping[str, Any] | None, record: CronRecord, expected_revision: int,
) -> CronRecord:
    """Validate a locked schedule transition before issuing its replacement."""

    if row is None:
        raise KeyError("schedule not found")
    current = _cron(row)
    if current.revision != expected_revision:
        raise CronStoreConflictError("schedule revision changed")
    if (current.key, current.task_name, current.timezone) != (record.key, record.task_name, record.timezone):
        raise CronStoreConflictError("schedule identity is immutable")
    if current.status == "cancelled":
        if record != current:
            raise CronStoreConflictError("cancelled schedule is terminal")
        return current
    return replace(
        record, arguments=_decode(_dump(record.arguments)),
        revision=current.revision+1, created_at=current.created_at,
    )


def _cron(row: Mapping[str, Any]) -> CronRecord:
    """Translate storage-specific names back to the public record shape."""

    values = dict(row)
    values["key"] = values.pop("schedule_key")
    values["arguments"] = _decode(values["arguments"])
    return CronRecord(**values)


def _dump(value: Any) -> str:
    """Serialize immutable mapping values using the public JSON contract."""

    return json.dumps(value, default=_json_container, allow_nan=False)


def _json_container(value: Any) -> Any:
    """Support frozen record containers without accepting arbitrary objects."""

    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError("task storage only supports JSON values")


def _dump_optional(value: Any) -> str | None:
    """Preserve SQL null separately from encoded values."""

    return None if value is None else _dump(value)


def _decode(value: Any) -> Any:
    """Allow default asyncpg JSON text and application-configured JSON codecs."""

    return json.loads(value) if isinstance(value, str) else value


def _require_limit(limit: int) -> None:
    """Bound every provider page before it reaches the database."""

    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer between 1 and 1000")


def _require_claim_options(now: float, lease_seconds: float, limit: int) -> None:
    """Require a useful lease duration and a bounded claim batch."""

    _require_limit(limit)
    if not math.isfinite(now) or not math.isfinite(lease_seconds) or lease_seconds <= 0:
        raise ValueError("lease requires finite now and positive lease_seconds")


def _require_finish_options(status: str, now: float, retry_at: float | None) -> None:
    """Reject ambiguous terminal/retry transitions before modifying storage."""

    if status not in {"pending", "completed", "failed"}:
        raise ValueError("finish status must be pending, completed, or failed")
    if status == "pending" and (retry_at is None or retry_at < now):
        raise ValueError("retry requires retry_at >= now")


def _require_occurrence(
    application_id: str, user_id: str, due_at: float, next_run_at: float, task: TaskRecord,
) -> None:
    """Keep atomic occurrence writes inside one owner and advancing timeline."""

    if (task.application_id, task.user_id) != (application_id, user_id):
        raise CronStoreConflictError("occurrence task belongs to another owner")
    if next_run_at <= due_at:
        raise ValueError("next_run_at must advance the schedule")


@asynccontextmanager
async def _mutation(operation: str, trigger: str) -> AsyncIterator[None]:
    """Emit privacy-safe outcomes after the surrounding transaction commits."""

    try:
        yield
    except Exception:
        _audit(operation, trigger, "failed")
        raise
    _audit(operation, trigger, "committed")


def _audit(operation: str, trigger: str, outcome: str) -> None:
    """Keep database payloads and exception text outside audit telemetry."""

    _AUDIT.info(
        operation, operation=operation, trigger=trigger,
        outcome=outcome, backend="postgres",
    )


__all__ = ["PostgresTaskStore"]
