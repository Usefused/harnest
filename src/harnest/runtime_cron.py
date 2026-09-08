"""Durable, user-owned cron job persistence for the task runtime."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Any
import uuid

from . import context
from .cron import (
    CronConflictError,
    CronJob,
    CronNotFoundError,
    CronRuntimeError,
    CronUnavailableError,
    _UNSET,
    _matches_schedule,
    _validate_task_call,
)
from .logging import get_logger
from .task import CompiledTask, TaskCallable, safe_task_arguments


_AUDIT = get_logger("cron.audit")
_JOB_FIELDS = """
schedule_id, schedule_key, expression, timezone, task_name, arguments, status,
user_id
"""
_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS harnest_cron_jobs (
    schedule_id text PRIMARY KEY,
    application_id text NOT NULL,
    user_id text NOT NULL,
    schedule_key text NOT NULL,
    expression text NOT NULL,
    timezone text NOT NULL DEFAULT 'UTC',
    task_name text NOT NULL,
    arguments jsonb NOT NULL,
    status text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (application_id, user_id, schedule_key),
    CHECK (timezone = 'UTC'),
    CHECK (status IN ('active', 'paused', 'cancelled'))
)
"""
_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS harnest_cron_jobs_active
ON harnest_cron_jobs (application_id, status, schedule_id)
"""
_INSERT_SQL = f"""
INSERT INTO harnest_cron_jobs(
    schedule_id, application_id, user_id, schedule_key, expression, timezone,
    task_name, arguments, status
)
VALUES (
    %(schedule_id)s, %(application_id)s, %(user_id)s, %(schedule_key)s,
    %(expression)s, 'UTC', %(task_name)s, %(arguments)s, 'active'
)
ON CONFLICT (application_id, user_id, schedule_key) DO UPDATE
SET updated_at=harnest_cron_jobs.updated_at
WHERE harnest_cron_jobs.expression=EXCLUDED.expression
  AND harnest_cron_jobs.task_name=EXCLUDED.task_name
  AND harnest_cron_jobs.arguments=EXCLUDED.arguments
RETURNING {_JOB_FIELDS}
"""
_GET_SQL = f"""
SELECT {_JOB_FIELDS}
FROM harnest_cron_jobs
WHERE schedule_id=%(schedule_id)s AND application_id=%(application_id)s
  AND user_id=%(user_id)s
"""
_LIST_SQL = f"""
SELECT {_JOB_FIELDS}
FROM harnest_cron_jobs
WHERE application_id=%(application_id)s AND user_id=%(user_id)s
  AND schedule_id > %(after)s
ORDER BY schedule_id
LIMIT %(limit)s
"""
_UPDATE_SQL = f"""
UPDATE harnest_cron_jobs
SET expression=%(expression)s, arguments=%(arguments)s, updated_at=now()
WHERE schedule_id=%(schedule_id)s AND application_id=%(application_id)s
  AND user_id=%(user_id)s AND status != 'cancelled'
RETURNING {_JOB_FIELDS}
"""
_SET_STATUS_SQL = f"""
UPDATE harnest_cron_jobs
SET status=%(status)s, updated_at=now()
WHERE schedule_id=%(schedule_id)s AND application_id=%(application_id)s
  AND user_id=%(user_id)s
  AND (status != 'cancelled' OR %(status)s = 'cancelled')
RETURNING {_JOB_FIELDS}
"""
_DELETE_SQL = """
DELETE FROM harnest_cron_jobs
WHERE schedule_id=%(schedule_id)s AND application_id=%(application_id)s
  AND user_id=%(user_id)s
RETURNING schedule_id
"""
_LIST_ACTIVE_SQL = f"""
SELECT {_JOB_FIELDS}
FROM harnest_cron_jobs
WHERE application_id=%(application_id)s AND status='active'
  AND schedule_id > %(after)s
ORDER BY schedule_id
LIMIT %(limit)s
"""
_PAUSE_STALE_SQL = """
UPDATE harnest_cron_jobs
SET status='paused', updated_at=now()
WHERE schedule_id=%(schedule_id)s AND application_id=%(application_id)s
  AND status='active'
RETURNING schedule_id
"""
_PAGE_SIZE = 100


class DynamicCronRuntime:
    """Own dynamic cron CRUD and dispatch over one task-runtime connector."""

    def __init__(
        self,
        application: Any,
        *,
        enabled: bool,
        enqueue: Callable[
            [CompiledTask, Mapping[str, Any], Mapping[str, Any], str], Awaitable[None]
        ],
    ) -> None:
        """Retain compiled task identity without opening the database eagerly."""

        self._application = application
        self._enabled = enabled
        self._enqueue = enqueue
        self._tasks = {item.name: item for item in application.tasks}
        self._app: Any | None = None

    async def start(self, app: Any) -> None:
        """Create the additive durable registry on the task-owned connector."""

        if self._app is not None and self._app is not app:
            raise CronRuntimeError("dynamic cron runtime is already started")
        self._app = app
        await app.connector.execute_query_async(query=_TABLE_SQL)
        await app.connector.execute_query_async(query=_INDEX_SQL)

    def close(self) -> None:
        """Revoke retained handles when the owning task runtime closes."""

        self._app = None

    async def create_dynamic_schedule(
        self,
        *,
        key: str,
        expression: str,
        task: TaskCallable[Any],
        arguments: Mapping[str, Any],
    ) -> CronJob:
        """Persist one idempotently keyed schedule in the active user's scope."""

        active = self._active_context()
        compiled = self._compiled_for(task)
        schedule_id = f"cron_{uuid.uuid4().hex}"
        values = {
            "schedule_id": schedule_id,
            "user_id": active.user_id,
            "schedule_key": key,
            "expression": expression,
            "task_name": compiled.name,
            "arguments": dict(arguments),
        }
        try:
            rows = await self._rows(_INSERT_SQL, **values)
        except Exception:
            _audit("create", schedule_id, "failed")
            raise
        if not rows:
            _audit("create", "conflict", "failed")
            raise CronConflictError(
                "cron job key already belongs to a different definition"
            )
        job = self._job(rows[0])
        _audit("create", job.id, "committed" if job.id == schedule_id else "unchanged")
        return job

    async def get_dynamic_schedule(self, schedule_id: str) -> CronJob | None:
        """Read one owner-scoped schedule without exposing cross-user existence."""

        active = self._active_context()
        rows = await self._rows(
            _GET_SQL, schedule_id=schedule_id, user_id=active.user_id
        )
        return None if not rows else self._job(rows[0])

    async def list_dynamic_schedules(
        self, *, after: str | None, limit: int
    ) -> tuple[CronJob, ...]:
        """Read one database-ordered page within the active user's scope."""

        active = self._active_context()
        rows = await self._rows(
            _LIST_SQL,
            user_id=active.user_id,
            after="" if after is None else after,
            limit=limit,
        )
        return tuple(self._job(row) for row in rows)

    async def update_dynamic_schedule(
        self,
        schedule_id: str,
        *,
        expression: str | None,
        arguments: Mapping[str, Any] | object,
    ) -> CronJob:
        """Replace validated mutable fields on one non-cancelled cron job."""

        current = await self._required(schedule_id)
        if current.status == "cancelled":
            raise CronConflictError("cancelled cron jobs cannot be updated")
        normalized = (
            safe_task_arguments(current.arguments)
            if arguments is _UNSET
            else safe_task_arguments(arguments)  # type: ignore[arg-type]
        )
        compiled = self._tasks.get(current.task_name)
        if compiled is None:
            raise CronConflictError("cron job target is unavailable in this deployment")
        _validate_task_call(compiled.authored, normalized)
        active = context.current()
        rows = await self._mutate(
            "update",
            schedule_id,
            _UPDATE_SQL,
            user_id=active.user_id,
            expression=current.expression if expression is None else expression,
            arguments=dict(normalized),
        )
        return self._job(rows[0])

    async def set_dynamic_schedule_status(
        self, schedule_id: str, status: str
    ) -> CronJob:
        """Apply a reversible pause or terminal cancellation for the active owner."""

        if status not in {"active", "paused", "cancelled"}:
            raise ValueError("cron job status is invalid")
        current = await self._required(schedule_id)
        if current.status == "cancelled" and status != "cancelled":
            raise CronConflictError("cancelled cron jobs cannot be resumed or paused")
        operation = _status_operation(status)
        if current.status == status:
            _audit(operation, schedule_id, "unchanged")
            return current
        active = context.current()
        rows = await self._mutate(
            operation,
            schedule_id,
            _SET_STATUS_SQL,
            user_id=active.user_id,
            status=status,
        )
        return self._job(rows[0])

    async def delete_dynamic_schedule(self, schedule_id: str) -> bool:
        """Permanently remove one cron job using application and owner predicates."""

        active = self._active_context()
        try:
            rows = await self._rows(
                _DELETE_SQL, schedule_id=schedule_id, user_id=active.user_id
            )
        except Exception:
            _audit("delete", schedule_id, "failed")
            raise
        changed = bool(rows)
        _audit("delete", schedule_id, "committed" if changed else "unchanged")
        return changed

    async def dispatch(self, timestamp: int) -> None:
        """Page active durable jobs and enqueue each matching UTC occurrence once."""

        if not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp < 0:
            raise CronRuntimeError("cron timestamp must be a non-negative integer")
        after = ""
        while True:
            rows = await self._rows(
                _LIST_ACTIVE_SQL, after=after, limit=_PAGE_SIZE
            )
            for row in rows:
                await self._dispatch_row(row, timestamp)
            if len(rows) < _PAGE_SIZE:
                return
            after = str(rows[-1]["schedule_id"])

    async def _dispatch_row(self, row: Mapping[str, Any], timestamp: int) -> None:
        """Resolve deployment ownership before committing one due occurrence."""

        job = self._job(row)
        compiled = self._tasks.get(job.task_name)
        if compiled is None or not _call_is_valid(compiled, job.arguments):
            await self._pause_stale(job.id)
            return
        if not _matches_schedule(job.expression, timestamp):
            return
        try:
            await self._enqueue(
                compiled,
                safe_task_arguments(job.arguments),
                self._snapshot(row, job.id, timestamp),
                f"{job.id}:{timestamp}",
            )
        except BaseException:
            _audit("enqueue", job.id, "failed", trigger="cron")
            raise
        _audit("enqueue", job.id, "committed", trigger="cron")

    async def _pause_stale(self, schedule_id: str) -> None:
        """Pause a job whose target is unavailable or signature-incompatible."""

        rows = await self._rows(_PAUSE_STALE_SQL, schedule_id=schedule_id)
        _audit(
            "reconcile",
            schedule_id,
            "committed" if rows else "unchanged",
            trigger="cron",
        )

    async def _required(self, schedule_id: str) -> CronJob:
        """Return one owner-scoped job or the public missing-record error."""

        job = await self.get_dynamic_schedule(schedule_id)
        if job is None:
            raise CronNotFoundError("cron job was not found")
        return job

    async def _mutate(
        self, operation: str, schedule_id: str, query: str, **values: Any
    ) -> Sequence[Any]:
        """Apply one owner mutation with consistent audit and missing semantics."""

        try:
            rows = await self._rows(query, schedule_id=schedule_id, **values)
        except Exception:
            _audit(operation, schedule_id, "failed")
            raise
        if not rows:
            _audit(operation, schedule_id, "failed")
            raise CronNotFoundError("cron job was not found")
        _audit(operation, schedule_id, "committed")
        return rows

    async def _rows(self, query: str, **values: Any) -> Sequence[Any]:
        """Apply application scope and normalize connector failures consistently."""

        app = self._require_ready()
        try:
            rows = await app.connector.execute_query_all_async(
                query=query, application_id=self._application.name, **values
            )
        except Exception as error:
            raise CronRuntimeError(
                f"cron job query failed with {type(error).__name__}"
            ) from None
        if not isinstance(rows, Sequence) or len(rows) > values.get("limit", 1):
            raise CronRuntimeError("cron job query returned an invalid result")
        return rows

    def _active_context(self) -> Any:
        """Require live invocation and long-lived scheduler ownership."""

        self._require_ready()
        if not self._enabled:
            raise CronUnavailableError(
                "dynamic cron is disabled in this runtime; use harnest serve"
            )
        return context.current()

    def _require_ready(self) -> Any:
        """Reject operations after the task connector has closed."""

        if self._app is None:
            raise CronUnavailableError("dynamic cron runtime is not started")
        return self._app

    def _compiled_for(self, task: TaskCallable[Any]) -> CompiledTask:
        """Resolve only a callable owned by this compiled application."""

        for compiled in self._tasks.values():
            if compiled.authored is task:
                return compiled
        raise CronConflictError("cron task does not belong to this runtime")

    def _job(self, row: Mapping[str, Any]) -> CronJob:
        """Project a private database row into an owner-bound public handle."""

        return CronJob(
            id=row["schedule_id"],
            key=row["schedule_key"],
            expression=row["expression"],
            timezone=row["timezone"],
            task_name=row["task_name"],
            arguments=row["arguments"],
            status=row["status"],
            _runtime=self,
        )

    def _snapshot(
        self, row: Mapping[str, Any], schedule_id: str, timestamp: int
    ) -> Mapping[str, Any]:
        """Build fresh current-runtime identity for one user-owned occurrence."""

        user_id = row.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise CronRuntimeError("cron job owner identity is invalid")
        occurrence_id = f"{schedule_id}:{timestamp}"
        return MappingProxyType(
            {
                "framework": self._application.framework,
                "agent_name": self._application.name,
                "invocation_id": occurrence_id,
                "user_id": user_id,
                "session_id": occurrence_id,
                "metadata": {},
            }
        )


def _call_is_valid(compiled: CompiledTask, arguments: Mapping[str, Any]) -> bool:
    """Classify deployment signature drift without blocking other schedules."""

    try:
        _validate_task_call(compiled.authored, arguments)
    except TypeError:
        return False
    return True


def _status_operation(status: str) -> str:
    """Map stored states to the mutation verb shown in audit logs."""

    return {"active": "resume", "paused": "pause", "cancelled": "cancel"}[status]


def _audit(
    operation: str, schedule: str, outcome: str, *, trigger: str = "agent"
) -> None:
    """Record mutations without schedule keys, expressions, or arguments."""

    event = f"task.cron.{operation}"
    _AUDIT.info(
        event,
        operation=event,
        trigger=trigger,
        outcome=outcome,
        backend="procrastinate",
        schedule=schedule,
    )


__all__: list[str] = []
