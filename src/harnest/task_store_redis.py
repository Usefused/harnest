"""Redis persistence for Harnest-managed durable task and cron execution."""

from __future__ import annotations

from dataclasses import fields
import hashlib
import json
import math
import secrets
import struct
from typing import Any

from .cron_storage import CronRecord, CronStore, CronStoreConflictError, cron_fingerprint
from .store_redis import RedisStore
from .task_storage import TaskRecord, TaskStore, TaskStoreConflictError, plain_json, task_fingerprint
from . import task_store_redis_scripts as scripts


class RedisTaskStore(RedisStore, TaskStore, CronStore):
    """Combined storage with atomic task leases and cron occurrence handoff.

    Tasks have no expiration: durable retention follows explicit lifecycle
    operations, independent of the session/checkpoint TTL configuration.
    Configure Redis persistence and replication for the required durability.
    """

    async def enqueue_task(self, record: TaskRecord) -> TaskRecord:
        """Persist a job once per scoped key, retaining immutable fingerprints."""

        result = await self._eval(
            scripts.ENQUEUE,
            self._enqueue_keys(record),
            (_task_dump(record), _task_identity(record), task_fingerprint(record)),
        )
        return _task_outcome(result)

    async def get_task(
        self, *, application_id: str, job_id: str, user_id: str | None = None
    ) -> TaskRecord | None:
        """Apply owner scope inside Redis before returning a single record."""

        raw = await self._eval(
            scripts.READ_SCOPED,
            (self._durable_key(application_id, "jobs"),),
            (job_id, user_id if user_id is not None else "", "trusted" if user_id is None else "owner"),
        )
        return _task_load(raw) if raw else None

    async def claim_tasks(
        self, *, application_id: str, queues: tuple[str, ...], now: float,
        lease_seconds: float, limit: int = 1,
    ) -> tuple[TaskRecord, ...]:
        """Claim bounded indexed work and recover expired attempts atomically."""

        _require_limit(limit)
        _require_lease(now, lease_seconds)
        keys = [self._durable_key(application_id, "jobs")]
        for queue in dict.fromkeys(queues):
            keys.extend(self._queue_keys(application_id, queue))
        if len(keys) == 1:
            return ()
        records = await self._eval(
            scripts.CLAIM, keys, (now, now + lease_seconds, limit, secrets.token_hex(16))
        )
        return tuple(_task_load(raw) for raw in records)

    async def renew_task_lease(
        self, *, application_id: str, job_id: str, lease_token: str,
        now: float, lease_seconds: float,
    ) -> bool:
        """Renew only the matching unexpired attempt's fenced queue lease."""

        _require_lease(now, lease_seconds)
        record = await self.get_task(application_id=application_id, job_id=job_id)
        if record is None:
            return False
        keys = (self._durable_key(application_id, "jobs"),
                self._queue_keys(application_id, record.queue)[1])
        return bool(await self._eval(
            scripts.RENEW, keys, (job_id, lease_token, now, now + lease_seconds)
        ))

    async def finish_task(
        self, *, application_id: str, job_id: str, lease_token: str,
        now: float, status: str, result: Any = None,
        failure_code: str | None = None, retry_at: float | None = None,
    ) -> bool:
        """Commit a fenced result or delayed retry and scrub terminal payloads."""

        _require_finish(status, now, retry_at)
        record = await self.get_task(application_id=application_id, job_id=job_id)
        if record is None:
            return False
        keys = (self._durable_key(application_id, "jobs"),
                *self._queue_keys(application_id, record.queue))
        return bool(await self._eval(
            scripts.FINISH, keys,
            (job_id, lease_token, now, status, _json(result), _json(failure_code),
             retry_at if retry_at is not None else 0),
        ))

    async def cancel_task(
        self, *, application_id: str, user_id: str, job_id: str, now: float
    ) -> bool:
        """Cancel scoped work and invalidate running claims in one mutation."""

        record = await self.get_task(
            application_id=application_id, job_id=job_id, user_id=user_id
        )
        if record is None:
            return False
        keys = (self._durable_key(application_id, "jobs"),
                *self._queue_keys(application_id, record.queue))
        return bool(await self._eval(scripts.CANCEL, keys, (job_id, user_id, now)))

    async def create_cron(self, record: CronRecord) -> CronRecord:
        """Atomically persist schedule identity, owner index, and due index."""

        result = await self._eval(
            scripts.CREATE_CRON, self._cron_keys(record.application_id, record.user_id),
            (_cron_dump(record), _identity(record.user_id, record.key),
             cron_fingerprint(record), _due_member(record.next_run_at, record.schedule_id)),
        )
        return _cron_outcome(result)

    async def get_cron(
        self, *, application_id: str, user_id: str, schedule_id: str
    ) -> CronRecord | None:
        """Read one schedule using an owner predicate evaluated by Redis."""

        raw = await self._eval(
            scripts.READ_SCOPED, (self._durable_key(application_id, "crons"),),
            (schedule_id, user_id, "owner"),
        )
        return _cron_load(raw) if raw else None

    async def list_crons(
        self, *, application_id: str, user_id: str,
        after: str | None = None, limit: int = 100,
    ) -> tuple[CronRecord, ...]:
        """Return a bounded lexicographic owner-index page in one Redis call."""

        _require_limit(limit)
        keys = (self._durable_key(application_id, "crons"),
                self._durable_key(application_id, "owner", _identity(user_id)))
        rows = await self._eval(
            scripts.LIST_CRONS, keys, ("-" if after is None else "(" + after, "+", limit, "owner")
        )
        return tuple(_cron_load(raw) for raw in rows)

    async def update_cron(
        self, record: CronRecord, *, expected_revision: int
    ) -> CronRecord:
        """Replace mutable schedule fields with revision and terminal fencing."""

        keys = (self._durable_key(record.application_id, "crons"),
                self._durable_key(record.application_id, "due"))
        result = await self._eval(
            scripts.UPDATE_CRON, keys,
            (_cron_dump(record), expected_revision, cron_fingerprint(record),
             _due_member(record.next_run_at, record.schedule_id)),
        )
        return _cron_outcome(result)

    async def delete_cron(
        self, *, application_id: str, user_id: str, schedule_id: str
    ) -> bool:
        """Remove one owner's record and its identity/query indexes atomically."""

        record = await self.get_cron(
            application_id=application_id, user_id=user_id, schedule_id=schedule_id
        )
        if record is None:
            return False
        return bool(await self._eval(
            scripts.DELETE_CRON, self._cron_keys(application_id, user_id),
            (schedule_id, user_id, _identity(user_id, record.key)),
        ))

    async def list_due_crons(
        self, *, application_id: str, now: float,
        after: tuple[float, str] | None = None, limit: int = 100,
    ) -> tuple[CronRecord, ...]:
        """Read due schedules from a time/ID index with stable keyset bounds."""

        _require_limit(limit)
        minimum = "-" if after is None else "(" + _due_member(*after)
        # IEEE positive float bits sort chronologically, retaining the entire
        # timestamp precision and allowing a deleted cursor to remain useful.
        # The delimiter successor includes every possible schedule ID at now,
        # including Unicode IDs; a character sentinel cannot cover all strings.
        maximum = "[" + _due_member(now, "")[:-1] + ";"
        keys = (self._durable_key(application_id, "crons"),
                self._durable_key(application_id, "due"))
        rows = await self._eval(scripts.LIST_CRONS, keys, (minimum, maximum, limit, "due"))
        return tuple(_cron_load(raw) for raw in rows)

    async def commit_cron_occurrence(
        self, *, application_id: str, user_id: str, schedule_id: str,
        expected_revision: int, due_at: float, next_run_at: float,
        task: TaskRecord,
    ) -> TaskRecord | None:
        """Enqueue and advance a fenced occurrence within one Lua transaction."""

        if task.application_id != application_id or task.user_id != user_id:
            raise CronStoreConflictError("cron occurrence task has a different owner")
        if next_run_at <= due_at:
            raise ValueError("next_run_at must advance the schedule")
        keys = (*self._enqueue_keys(task), self._durable_key(application_id, "crons"),
                self._durable_key(application_id, "due"))
        outcome = await self._eval(
            scripts.COMMIT_OCCURRENCE, keys,
            (schedule_id, user_id, expected_revision, due_at, next_run_at,
             _due_member(next_run_at, schedule_id), _task_dump(task),
             _task_identity(task), task_fingerprint(task)),
        )
        if _text(outcome[0]) == "cron-conflict":
            raise CronStoreConflictError("cron occurrence task has a different target")
        return None if _text(outcome[0]) == "stale" else _task_outcome(outcome)

    def _durable_key(self, application_id: str, *parts: str) -> str:
        """Place all transactional keys for an application in one cluster slot."""

        slot = _identity(self._prefix, application_id)
        return ":".join(("harnest-tasks", "{" + slot + "}", *parts))

    def _queue_keys(self, application_id: str, queue: str) -> tuple[str, str]:
        """Return runnable and leased indexes without exposing authored names."""

        digest = _identity(queue)
        return (self._durable_key(application_id, "ready", digest),
                self._durable_key(application_id, "leased", digest))

    def _enqueue_keys(self, record: TaskRecord) -> tuple[str, ...]:
        """Keep enqueue and occurrence scripts on the same transaction layout."""

        return (self._durable_key(record.application_id, "jobs"),
                self._durable_key(record.application_id, "idempotency"),
                self._queue_keys(record.application_id, record.queue)[0])

    def _cron_keys(self, application_id: str, user_id: str) -> tuple[str, ...]:
        """Return schedule record, identity, owner, and due indexes in order."""

        return (self._durable_key(application_id, "crons"),
                self._durable_key(application_id, "cron-identities"),
                self._durable_key(application_id, "owner", _identity(user_id)),
                self._durable_key(application_id, "due"))


def _json(value: Any) -> str:
    """Encode private payloads before Lua can interpret their value types."""

    return json.dumps(plain_json(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _identity(*parts: str) -> str:
    """Hash an unambiguous tuple rather than concatenating untrusted names."""

    return hashlib.sha256(_json(parts).encode()).hexdigest()


def _text(value: Any) -> str:
    """Accept both binary and decode_responses Redis clients."""

    return value.decode() if isinstance(value, bytes) else str(value)


def _task_identity(record: TaskRecord) -> str:
    """Preserve per-application, per-owner, per-task idempotency scope."""

    if record.idempotency_key is None:
        return ""
    return _identity(record.user_id, record.task_name, record.idempotency_key)


def _task_dump(record: TaskRecord) -> str:
    """Serialize metadata separately from opaque JSON task values."""

    value = {field.name: getattr(record, field.name) for field in fields(record)}
    value["arguments"] = _json(dict(record.arguments))
    value["invocation"] = _json(None if record.invocation is None else dict(record.invocation))
    value["agent_permissions"] = _json(record.agent_permissions)
    value["result"] = _json(record.result)
    return _json(value)


def _task_load(raw: Any) -> TaskRecord:
    """Decode private payloads without exposing Redis persistence metadata."""

    value = json.loads(raw)
    value.pop("_fingerprint", None)
    for name in ("arguments", "invocation", "agent_permissions", "result"):
        value[name] = json.loads(value[name])
    if value["agent_permissions"] is not None:
        value["agent_permissions"] = tuple(value["agent_permissions"])
    return TaskRecord(**value)


def _cron_dump(record: CronRecord) -> str:
    """Keep cron arguments opaque across Lua metadata transitions."""

    value = {field.name: getattr(record, field.name) for field in fields(record)}
    value["arguments"] = _json(dict(record.arguments))
    return _json(value)


def _cron_load(raw: Any) -> CronRecord:
    """Restore authored arguments and discard private index metadata."""

    value = json.loads(raw)
    value.pop("_fingerprint", None)
    value.pop("_due_member", None)
    value["arguments"] = json.loads(value["arguments"])
    return CronRecord(**value)


def _task_outcome(outcome: Any) -> TaskRecord:
    """Translate transaction conflicts into the public task-store exception."""

    if _text(outcome[0]) != "ok":
        raise TaskStoreConflictError("task identity has a different definition")
    return _task_load(outcome[1])


def _cron_outcome(outcome: Any) -> CronRecord:
    """Preserve the contract distinction between a missing record and conflict."""

    status = _text(outcome[0])
    if status == "missing":
        raise KeyError("schedule does not exist")
    if status != "ok":
        raise CronStoreConflictError("schedule identity or revision conflicts")
    return _cron_load(outcome[1])


def _due_member(timestamp: float, schedule_id: str) -> str:
    """Encode nonnegative epoch seconds into an exact lexicographic cursor."""

    if not math.isfinite(timestamp) or timestamp < 0:
        raise ValueError("schedule timestamps must be finite nonnegative epoch seconds")
    return struct.pack(">d", float(timestamp)).hex() + ":" + schedule_id


def _require_limit(limit: int) -> None:
    """Bound Redis work explicitly before entering an atomic Lua operation."""

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")


def _require_lease(now: float, lease_seconds: float) -> None:
    """Reject lease values which could strand work or bypass expiration."""

    if not math.isfinite(now) or not math.isfinite(lease_seconds) or lease_seconds <= 0:
        raise ValueError("lease times must be finite and duration positive")


def _require_finish(status: str, now: float, retry_at: float | None) -> None:
    """Validate terminal and retry transitions before opening a transaction."""

    if status not in {"completed", "failed", "pending"} or not math.isfinite(now):
        raise ValueError("finish requires a valid state and timestamp")
    if status == "pending" and (
        retry_at is None or not math.isfinite(retry_at) or retry_at < now
    ):
        raise ValueError("pending transition requires a finite retry_at >= now")


__all__ = ["RedisTaskStore"]
