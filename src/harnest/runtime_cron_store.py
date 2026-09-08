"""Provider-neutral cron policy and atomic occurrence dispatch."""

from __future__ import annotations

from dataclasses import replace
import time
from typing import Any, Mapping
import uuid

from . import context
from .cron import (
    CronConflictError, CronJob, CronNotFoundError, CronRuntimeError, CronUnavailableError,
    _UNSET, _validate_schedule, _validate_task_call,
)
from .cron_storage import CronRecord, CronStoreConflictError
from .logging import get_logger
from .task import safe_task_arguments


_AUDIT = get_logger("cron.audit")


def next_occurrence(expression: str, after: float) -> float:
    """Calculate the next UTC occurrence independently of the database driver."""

    from croniter import croniter

    _validate_schedule(expression)
    return float(croniter(expression, after).get_next(float))


class StoredCronRuntime:
    """Enforce authoring policy while providers own atomic durable transitions."""

    def __init__(self, manager: Any, store: Any, *, enabled: bool) -> None:
        """Bind one shared task/cron provider without opening resources."""

        self._manager = manager
        self._store = store
        self._enabled = enabled
        self._application = manager.application
        self._tasks = {item.name: item for item in self._application.tasks}

    def _owner(self) -> str:
        """Require a serving runtime and current user before any authored query."""

        self._manager._require_ready()
        if not self._enabled or self._store is None:
            raise CronUnavailableError("configure lifecycle.storage.cron and use harnest serve")
        return context.current().user_id

    def _job(self, record: CronRecord) -> CronJob:
        """Return a scoped handle without exposing provider records to tools."""

        return CronJob(
            id=record.schedule_id, key=record.key, expression=record.expression,
            task_name=record.task_name, arguments=record.arguments,
            status=record.status, timezone=record.timezone, _runtime=self,
        )

    async def _record(self, schedule_id: str) -> CronRecord | None:
        """Fetch one schedule with both application and user predicates."""

        owner = self._owner()
        return await _provider_call(self._store.get_cron(
            application_id=self._application.name, user_id=owner,
            schedule_id=schedule_id,
        ))

    async def _required(self, schedule_id: str) -> CronRecord:
        """Give absent and inaccessible records the same public error."""

        record = await self._record(schedule_id)
        if record is None:
            raise CronNotFoundError("cron job was not found")
        return record

    async def create_dynamic_schedule(
        self, *, key: str, expression: str, task: Any, arguments: Mapping[str, Any]
    ) -> CronJob:
        """Create one user-owned schedule with provider-enforced idempotency."""

        owner = self._owner()
        compiled = self._manager._compiled_for(task)
        now = time.time()
        record = CronRecord(
            schedule_id=f"cron_{uuid.uuid4().hex}", application_id=self._application.name,
            user_id=owner, key=key, expression=expression, task_name=compiled.name,
            arguments=safe_task_arguments(arguments), next_run_at=next_occurrence(expression, now),
            created_at=now, updated_at=now,
        )
        return self._job(await self._mutation("create", self._store.create_cron(record)))

    async def get_dynamic_schedule(self, schedule_id: str) -> CronJob | None:
        """Read an immutable snapshot of one owned job."""

        record = await self._record(schedule_id)
        return None if record is None else self._job(record)

    async def list_dynamic_schedules(self, *, after: str | None, limit: int) -> tuple[CronJob, ...]:
        """Delegate ordering, owner filtering, and pagination to the provider."""

        owner = self._owner()
        records = await _provider_call(self._store.list_crons(
            application_id=self._application.name, user_id=owner, after=after, limit=limit,
        ))
        return tuple(self._job(record) for record in records)

    async def update_dynamic_schedule(
        self, schedule_id: str, *, expression: str | None, arguments: Any = _UNSET
    ) -> CronJob:
        """Apply a revision-checked edit and recalculate its future UTC occurrence."""

        current = await self._required(schedule_id)
        if current.status == "cancelled":
            raise CronConflictError("cancelled cron jobs cannot be updated")
        compiled = self._tasks.get(current.task_name)
        if compiled is None:
            raise CronConflictError("cron target is unavailable in this deployment")
        values = safe_task_arguments(current.arguments if arguments is _UNSET else arguments)
        _validate_task_call(compiled.authored, values)
        now = time.time()
        expression = current.expression if expression is None else expression
        _validate_schedule(expression)
        record = replace(
            current, expression=expression, arguments=values, updated_at=now,
            next_run_at=(current.next_run_at if expression == current.expression else next_occurrence(expression, now)),
        )
        return self._job(await self._save("update", record))

    async def set_dynamic_schedule_status(self, schedule_id: str, status: str) -> CronJob:
        """Pause, resume, or terminally cancel one owner-scoped schedule."""

        current = await self._required(schedule_id)
        if status not in {"active", "paused", "cancelled"}:
            raise ValueError("invalid cron status")
        if current.status == "cancelled" and status != "cancelled":
            raise CronConflictError("cancelled cron jobs cannot be resumed or paused")
        if current.status == status:
            return self._job(current)
        now = time.time()
        record = replace(
            current, status=status, updated_at=now,
            next_run_at=(next_occurrence(current.expression, now) if status == "active" else current.next_run_at),
        )
        return self._job(await self._save(status, record))

    async def delete_dynamic_schedule(self, schedule_id: str) -> bool:
        """Delete one owned schedule without cancelling already committed jobs."""

        owner = self._owner()
        try:
            changed = await _provider_call(self._store.delete_cron(
                application_id=self._application.name, user_id=owner, schedule_id=schedule_id,
            ))
        except Exception:
            _audit("delete", "failed")
            raise
        _audit("delete", "committed" if changed else "unchanged")
        return changed

    async def _save(self, operation: str, record: CronRecord) -> CronRecord:
        """Keep optimistic concurrency and audit handling consistent for mutations."""

        return await self._mutation(operation, self._store.update_cron(record, expected_revision=record.revision))

    async def _mutation(self, operation: str, call: Any) -> CronRecord:
        """Translate provider conflicts without exposing private values."""

        try:
            result = await _provider_call(call)
        except Exception:
            _audit(operation, "failed")
            raise
        _audit(operation, "committed")
        return result

    async def dispatch(self, now: float) -> None:
        """Drain a bounded due page; persisted next-run cursors survive restarts."""

        if not self._enabled or self._store is None:
            return
        records = await self._store.list_due_crons(application_id=self._application.name, now=now, limit=100)
        for record in records:
            await self._dispatch_record(record)

    async def _dispatch_record(self, record: CronRecord) -> None:
        """Commit an occurrence and advance its schedule in the same provider operation."""

        compiled = self._tasks.get(record.task_name)
        if compiled is None or not _valid_call(compiled, record.arguments):
            try:
                await self._save("reconcile", replace(record, status="paused", updated_at=time.time()))
            except CronConflictError:
                pass
            return
        task = self._manager._cron_task_record(compiled, record)
        try:
            result = await self._store.commit_cron_occurrence(
                application_id=record.application_id, user_id=record.user_id,
                schedule_id=record.schedule_id, expected_revision=record.revision,
                due_at=record.next_run_at, next_run_at=next_occurrence(record.expression, record.next_run_at), task=task,
            )
        except Exception:
            _audit("enqueue", "failed")
            raise
        if result is not None:
            _audit("enqueue", "committed")


def _valid_call(compiled: Any, arguments: Mapping[str, Any]) -> bool:
    """Pause persisted schedules when their authored target signature changes."""

    try:
        _validate_task_call(compiled.authored, arguments)
    except TypeError:
        return False
    return True


def _audit(operation: str, outcome: str) -> None:
    """Emit provider-independent mutation signals without customer payloads."""

    trigger = "cron" if operation in {"enqueue", "reconcile"} else "agent"
    _AUDIT.info(f"task.cron.{operation}", operation=operation, trigger=trigger, outcome=outcome)


async def _provider_call(call: Any) -> Any:
    """Prevent database diagnostics and private parameters escaping through tools."""

    try:
        return await call
    except CronStoreConflictError:
        raise CronConflictError("cron definition or revision conflicts with stored state") from None
    except KeyError:
        raise CronNotFoundError("cron job was not found") from None
    except Exception:
        raise CronRuntimeError("cron storage operation failed") from None
