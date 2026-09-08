"""In-process task/cron reference provider for tests and local development."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import fields, replace
from typing import Any
import uuid

from .cron_storage import CronRecord, CronStoreConflictError, cron_fingerprint
from .task_storage import TaskRecord, TaskStoreConflictError, task_fingerprint


class MemoryTaskStore:
    """Implement atomic contracts in one process; records do not survive restart."""

    def __init__(self) -> None:
        """Create isolated indexes and one transaction lock for both capabilities."""

        self._lock = asyncio.Lock()
        self._tasks: dict[tuple[str, str], TaskRecord] = {}
        self._task_fingerprints: dict[tuple[str, str], str] = {}
        self._idempotency: dict[tuple[str, str, str, str], tuple[str, str]] = {}
        self._crons: dict[tuple[str, str], CronRecord] = {}
        self._cron_keys: dict[tuple[str, str, str], tuple[str, str]] = {}

    async def start(self) -> None:
        """Satisfy lifecycle ownership without opening external resources."""

    async def close(self) -> None:
        """Release no resources; in-memory data lives only with this instance."""

    async def enqueue_task(self, record: TaskRecord) -> TaskRecord:
        """Commit an isolated task snapshot and its deduplication identity."""

        async with self._lock:
            return _copy_record(self._enqueue(record))

    def _enqueue(self, record: TaskRecord) -> TaskRecord:
        """Apply enqueue inside the caller's existing transaction lock."""

        key = (record.application_id, record.job_id)
        identity = _task_identity(record)
        target = self._idempotency.get(identity, key)
        fingerprint = task_fingerprint(record)
        existing = self._tasks.get(target)
        if existing is not None:
            if existing.user_id != record.user_id or self._task_fingerprints[target] != fingerprint:
                raise TaskStoreConflictError("task identity has a different definition")
            return existing
        # An idempotency key must not allow overwriting an independently owned ID.
        if key in self._tasks:
            raise TaskStoreConflictError("task ID already exists")
        self._tasks[key] = _copy_record(record)
        self._task_fingerprints[key] = fingerprint
        if record.idempotency_key is not None:
            self._idempotency[identity] = key
        return self._tasks[key]

    async def get_task(
        self, *, application_id: str, job_id: str, user_id: str | None = None
    ) -> TaskRecord | None:
        """Read one detached snapshot through the caller's ownership scope."""

        async with self._lock:
            record = self._tasks.get((application_id, job_id))
            if record is None or (user_id is not None and record.user_id != user_id):
                return None
            return _copy_record(record)

    async def claim_tasks(
        self, *, application_id: str, queues: tuple[str, ...], now: float,
        lease_seconds: float, limit: int = 1,
    ) -> tuple[TaskRecord, ...]:
        """Claim due work, reclaim expired leases and retire exhausted attempts."""

        _validate_claim_options(now, lease_seconds, limit)
        async with self._lock:
            candidates = sorted(
                (item for item in self._tasks.values()
                 if item.application_id == application_id and item.queue in queues
                 and _is_due(item, now)),
                key=lambda item: (item.scheduled_at, item.job_id),
            )
            return self._claim_candidates(candidates, now, lease_seconds, limit)

    def _claim_candidates(
        self, candidates: list[TaskRecord], now: float,
        lease_seconds: float, limit: int,
    ) -> tuple[TaskRecord, ...]:
        """Fence new attempts while terminalizing expired exhausted work."""

        claimed = []
        for record in candidates:
            key = (record.application_id, record.job_id)
            if record.attempt >= record.max_retries + 1:
                self._tasks[key] = _terminal(record, "failed", now, failure_code="task_failed")
                continue
            record = replace(
                record, status="running", attempt=record.attempt + 1,
                lease_token=uuid.uuid4().hex, lease_expires_at=now + lease_seconds,
                updated_at=now,
            )
            self._tasks[key] = record
            claimed.append(_copy_record(record))
            if len(claimed) == limit:
                break
        return tuple(claimed)

    async def renew_task_lease(
        self, *, application_id: str, job_id: str, lease_token: str,
        now: float, lease_seconds: float,
    ) -> bool:
        """Extend only a lease that still grants execution ownership."""

        _validate_claim_options(now, lease_seconds, 1)
        async with self._lock:
            key = (application_id, job_id)
            record = self._tasks.get(key)
            if not _owns_lease(record, lease_token, now):
                return False
            self._tasks[key] = replace(record, lease_expires_at=now + lease_seconds, updated_at=now)
            return True

    async def finish_task(
        self, *, application_id: str, job_id: str, lease_token: str,
        now: float, status: str, result: Any = None,
        failure_code: str | None = None, retry_at: float | None = None,
    ) -> bool:
        """Commit an outcome only while the exact execution lease remains valid."""

        _validate_finish(status, now, retry_at)
        async with self._lock:
            key = (application_id, job_id)
            record = self._tasks.get(key)
            if not _owns_lease(record, lease_token, now):
                return False
            self._tasks[key] = _finish(record, status, now, result, failure_code, retry_at)
            return True

    async def cancel_task(
        self, *, application_id: str, user_id: str, job_id: str, now: float
    ) -> bool:
        """Cancel one owner's active work and revoke any in-flight attempt."""

        async with self._lock:
            key = (application_id, job_id)
            record = self._tasks.get(key)
            if record is None or record.user_id != user_id or record.status not in {"pending", "running"}:
                return False
            self._tasks[key] = _terminal(record, "cancelled", now, failure_code="task_cancelled")
            return True

    async def create_cron(self, record: CronRecord) -> CronRecord:
        """Create a schedule or return an exact keyed retry without reactivation."""

        async with self._lock:
            key = (record.application_id, record.schedule_id)
            identity = (record.application_id, record.user_id, record.key)
            target = self._cron_keys.get(identity, key)
            existing = self._crons.get(target)
            if existing is not None:
                if _cron_identity(existing) != _cron_identity(record) or cron_fingerprint(existing) != cron_fingerprint(record):
                    raise CronStoreConflictError("cron identity has a different definition")
                return _copy_record(existing)
            self._crons[key] = _copy_record(record)
            self._cron_keys[identity] = key
            return _copy_record(record)

    async def get_cron(
        self, *, application_id: str, user_id: str, schedule_id: str
    ) -> CronRecord | None:
        """Read a detached schedule through both ownership predicates."""

        async with self._lock:
            record = self._crons.get((application_id, schedule_id))
            if record is None or record.user_id != user_id:
                return None
            return _copy_record(record)

    async def list_crons(
        self, *, application_id: str, user_id: str,
        after: str | None = None, limit: int = 100,
    ) -> tuple[CronRecord, ...]:
        """Return one bounded page from this in-memory reference collection."""

        _validate_limit(limit)
        async with self._lock:
            records = sorted(
                (item for item in self._crons.values() if item.application_id == application_id
                 and item.user_id == user_id and item.schedule_id > (after or "")),
                key=lambda item: item.schedule_id,
            )
            return tuple(_copy_record(item) for item in records[:limit])

    async def update_cron(
        self, record: CronRecord, *, expected_revision: int
    ) -> CronRecord:
        """Replace allowed fields only at the current scoped revision."""

        async with self._lock:
            key = (record.application_id, record.schedule_id)
            existing = self._crons.get(key)
            _validate_cron_update(existing, record, expected_revision)
            if existing.status == "cancelled":
                return _copy_record(existing)
            updated = replace(record, revision=existing.revision + 1)
            self._crons[key] = _copy_record(updated)
            return _copy_record(updated)

    async def delete_cron(
        self, *, application_id: str, user_id: str, schedule_id: str
    ) -> bool:
        """Remove one owner's schedule and its reusable authored key."""

        async with self._lock:
            key = (application_id, schedule_id)
            record = self._crons.get(key)
            if record is None or record.user_id != user_id:
                return False
            del self._crons[key]
            del self._cron_keys[_cron_identity(record)]
            return True

    async def list_due_crons(
        self, *, application_id: str, now: float,
        after: tuple[float, str] | None = None, limit: int = 100,
    ) -> tuple[CronRecord, ...]:
        """Read active due schedules with a stable timestamp-and-ID cursor."""

        _validate_limit(limit)
        async with self._lock:
            records = sorted(
                (item for item in self._crons.values()
                 if _cron_is_due(item, application_id, now, after)),
                key=lambda item: (item.next_run_at, item.schedule_id),
            )
            return tuple(_copy_record(item) for item in records[:limit])

    async def commit_cron_occurrence(
        self, *, application_id: str, user_id: str, schedule_id: str,
        expected_revision: int, due_at: float, next_run_at: float,
        task: TaskRecord,
    ) -> TaskRecord | None:
        """Use one lock for revision check, task insert and schedule advancement."""

        if next_run_at <= due_at:
            raise ValueError("cron next_run_at must advance")
        async with self._lock:
            key = (application_id, schedule_id)
            record = self._crons.get(key)
            if not _matches_occurrence(record, user_id, expected_revision, due_at):
                return None
            _validate_occurrence_task(record, task)
            queued = self._enqueue(task)
            self._crons[key] = replace(
                record, next_run_at=next_run_at, revision=record.revision + 1,
                updated_at=task.created_at,
            )
            return _copy_record(queued)


def _task_identity(record: TaskRecord) -> tuple[str, str, str, str]:
    """Scope submitted idempotency keys to the owner and authored task."""

    return record.application_id, record.user_id, record.task_name, record.idempotency_key or ""


def _cron_identity(record: CronRecord) -> tuple[str, str, str]:
    """Return the immutable authored schedule key scope."""

    return record.application_id, record.user_id, record.key


def _copy_value(value: Any) -> Any:
    """Detach JSON containers, including read-only runtime mappings."""

    if isinstance(value, Mapping):
        return {key: _copy_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_copy_value(item) for item in value)
    return value


def _copy_record(record: Any) -> Any:
    """Avoid sharing caller-owned mutable containers with persisted records."""

    return replace(record, **{item.name: _copy_value(getattr(record, item.name)) for item in fields(record)})


def _is_due(record: TaskRecord, now: float) -> bool:
    """Classify pending work and expired running work for atomic claims."""

    if record.status == "pending":
        return record.scheduled_at <= now
    return record.status == "running" and record.lease_expires_at is not None and record.lease_expires_at <= now


def _owns_lease(record: TaskRecord | None, token: str, now: float) -> bool:
    """Reject stale attempts even before another worker reclaims the job."""

    return (record is not None and record.status == "running"
            and record.lease_token == token and record.lease_expires_at is not None
            and record.lease_expires_at > now)


def _terminal(
    record: TaskRecord, status: str, now: float, *, result: Any = None,
    failure_code: str | None = None,
) -> TaskRecord:
    """Retain recoverable outcome while erasing execution inputs and authority."""

    return replace(
        record, status=status, result=_copy_value(result), failure_code=failure_code,
        arguments={}, invocation=None, agent_permissions=None,
        lease_token=None, lease_expires_at=None, updated_at=now,
    )


def _finish(
    record: TaskRecord, status: str, now: float, result: Any,
    failure_code: str | None, retry_at: float | None,
) -> TaskRecord:
    """Apply shared retry limits before choosing payload retention policy."""

    if status != "pending":
        return _terminal(record, status, now, result=result, failure_code=failure_code)
    if record.attempt > record.max_retries:
        return _terminal(record, "failed", now, failure_code="task_failed")
    return replace(record, status="pending", scheduled_at=retry_at,
                   lease_token=None, lease_expires_at=None, updated_at=now)


def _validate_limit(limit: int) -> None:
    """Reject unbounded or ambiguous page and claim sizes."""

    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000")


def _validate_claim_options(now: float, lease_seconds: float, limit: int) -> None:
    """Require forward-moving execution leases and a bounded claim batch."""

    import math
    _validate_limit(limit)
    if not math.isfinite(now) or not math.isfinite(lease_seconds) or lease_seconds <= 0:
        raise ValueError("lease requires finite now and positive lease_seconds")


def _validate_finish(status: str, now: float, retry_at: float | None) -> None:
    """Prevent invalid outcome states from entering the reference store."""

    if status not in {"pending", "completed", "failed"}:
        raise ValueError("finish status must be pending, completed or failed")
    if status == "pending" and (retry_at is None or retry_at < now):
        raise ValueError("retry requires retry_at >= now")


def _validate_cron_update(
    existing: CronRecord | None, incoming: CronRecord, expected_revision: int
) -> None:
    """Enforce owner, immutable identity, revision and terminal cancellation."""

    if existing is None or existing.user_id != incoming.user_id:
        raise KeyError(incoming.schedule_id)
    if _cron_identity(existing) != _cron_identity(incoming) or existing.task_name != incoming.task_name:
        raise CronStoreConflictError("cron identity is immutable")
    if existing.revision != expected_revision:
        raise CronStoreConflictError("cron revision changed")
    if existing.status == "cancelled" and incoming != existing:
        raise CronStoreConflictError("cancelled cron cannot change")


def _cron_is_due(
    record: CronRecord, application_id: str, now: float,
    after: tuple[float, str] | None,
) -> bool:
    """Apply active and cursor predicates for the in-memory due query."""

    return (record.application_id == application_id and record.status == "active"
            and record.next_run_at <= now
            and (after is None or (record.next_run_at, record.schedule_id) > after))


def _matches_occurrence(
    record: CronRecord | None, user_id: str, revision: int, due_at: float
) -> bool:
    """Recheck the complete claimed schedule identity before enqueueing."""

    return (record is not None and record.user_id == user_id and record.status == "active"
            and record.revision == revision and record.next_run_at == due_at)


def _validate_occurrence_task(record: CronRecord, task: TaskRecord) -> None:
    """Keep the atomic handoff within the selected schedule's owner and target."""

    if (record.application_id, record.user_id, record.task_name) != (task.application_id, task.user_id, task.task_name):
        raise CronStoreConflictError("cron occurrence task has different ownership")
    if record.arguments != task.arguments:
        raise CronStoreConflictError("cron occurrence task has different arguments")


__all__ = ["MemoryTaskStore"]
