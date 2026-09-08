"""Harnest-owned workers over public database-neutral persistence contracts."""

from __future__ import annotations

import asyncio
from contextvars import Context
from dataclasses import replace
import hashlib
import time
from typing import Any, Mapping
import uuid

from .cron_storage import CronRecord, CronStoreConflictError
from . import context
from .context import ContextUnavailableError
from .logging import get_logger
from .runtime_cron_store import StoredCronRuntime, next_occurrence
from .runtime_task import (
    TaskRuntimeManager, TaskRuntimeError, _capture_invocation,
    _capture_agent_permissions, _native_idempotency_key, _validated_snapshot,
    _validated_agent_permissions,
)
from .task import TaskHandle, bind_task_runtime, release_task_runtime, safe_task_arguments, safe_task_result
from .task_storage import TaskRecord, TaskStoreConflictError


_AUDIT = get_logger("task.audit")
_LEASE_SECONDS = 30.0


class ProviderTaskRuntimeManager(TaskRuntimeManager):
    """Reuse managed execution/continuations while replacing queue-specific I/O.

    The inherited invocation machinery is shared with the retained Procrastinate
    adapter so old databases can drain during adoption of explicit providers.
    No Procrastinate library, schema, or connection is used by this manager.
    """

    def __init__(self, application: Any, *, manage_storage: bool = True, **options: Any) -> None:
        """Bind explicit storage and retain ownership of only the worker loop."""

        super().__init__(application, **options)
        self._store = application.runtime_capabilities.task_store
        if self._store is None:
            raise TaskRuntimeError("configure lifecycle.storage.tasks")
        self._manage_storage = manage_storage
        self._dynamic_cron = StoredCronRuntime(
            self, application.runtime_capabilities.cron_store, enabled=self._enable_cron,
        )
        if self._crons and application.runtime_capabilities.cron_store is None:
            raise TaskRuntimeError("compiled cron declarations require lifecycle.storage.cron")
        self._active_jobs: set[asyncio.Task[Any]] = set()

    async def _start_locked(self) -> None:
        """Start provider-owned workers only after managed resources are ready."""

        if self._manage_storage:
            await self._store.start()
        if self._enable_cron:
            await self._register_static_schedules()
        for compiled in self._tasks:
            bind_task_runtime(compiled.authored, self)
        # Lazy startup can happen inside a tool. Background jobs must not inherit
        # that caller's ContextVars or authorization capabilities.
        self._worker = Context().run(asyncio.create_task, self._run_worker(), name=f"harnest-tasks-{self._application.name}")
        self._worker.add_done_callback(self._worker_done)

    def _require_ready(self) -> None:
        """Surface worker failure without assuming a database client type."""

        if self._state != "started":
            raise TaskRuntimeError("task runtime is not started")
        if self._worker_failure is not None:
            raise self._worker_failure

    def _audit_runtime(self, operation: str, trigger: str, outcome: str) -> None:
        """Keep shared continuation diagnostics independent of the legacy engine."""

        _audit(operation, trigger, outcome)

    async def defer(self, task_value: Any, arguments: Mapping[str, Any], *, idempotency_key: str | None, schedule_in: float | None) -> TaskHandle:
        """Persist a scoped job with stable replay identity and private arguments."""

        await self.start()
        compiled = self._compiled_for(task_value)
        snapshot = _capture_invocation()
        if idempotency_key is None:
            idempotency_key = _native_idempotency_key(self._application, compiled, arguments, snapshot)
        return await self._defer_compiled(
            compiled, arguments, snapshot, _capture_agent_permissions(),
            trigger="agent" if snapshot is not None else "user",
            idempotency_key=idempotency_key, schedule_in=schedule_in,
        )

    def _task_record(self, compiled: Any, arguments: Mapping[str, Any], snapshot: Mapping[str, Any] | None, permissions: Any, *, trigger: str, key: str | None, scheduled_at: float) -> TaskRecord:
        """Derive opaque IDs with owner scope before handing content to storage."""

        owner = self._automation_user_id if snapshot is None else snapshot["user_id"]
        identity = uuid.uuid4().hex if key is None else hashlib.sha256(
            repr((self._application.name, owner, compiled.name, key)).encode()
        ).hexdigest()
        now = time.time()
        return TaskRecord(
            job_id=identity, application_id=self._application.name, user_id=owner,
            task_name=compiled.name, queue=compiled.queue,
            arguments=safe_task_arguments(arguments),
            invocation=None if snapshot is None else dict(snapshot),
            agent_permissions=None if permissions is None else tuple(sorted(permissions)),
            trigger=trigger, scheduled_at=scheduled_at, max_retries=compiled.max_retries,
            idempotency_key=key, created_at=now, updated_at=now,
        )

    async def _defer_compiled(self, compiled: Any, arguments: Mapping[str, Any], snapshot: Mapping[str, Any] | None, agent_permissions: Any = None, *, trigger: str, idempotency_key: str | None, schedule_in: float | None) -> TaskHandle:
        """Atomically enqueue the job and payload through the selected provider."""

        self._require_ready()
        record = self._task_record(
            compiled, arguments, snapshot, agent_permissions, trigger=trigger,
            key=idempotency_key, scheduled_at=time.time() + (schedule_in or 0),
        )
        try:
            stored = await _provider_call(self._store.enqueue_task(record))
        except Exception:
            _audit("defer", trigger, "failed")
            raise
        _audit("defer", trigger, "committed")
        return TaskHandle(stored.job_id, compiled.name, self, stored.job_id, trigger)

    async def status(self, handle: TaskHandle) -> str:
        """Keep released TaskHandle status names independent of provider states."""

        record = await self._handle_record(handle)
        return {"pending": "todo", "running": "doing", "completed": "succeeded", "cancelled": "aborted"}.get(record.status, record.status)

    async def _handle_record(self, handle: TaskHandle) -> TaskRecord:
        """Validate runtime and compiled target before reading an opaque job."""

        self._require_handle(handle)
        self._require_ready()
        try:
            user_id = context.current().user_id
        except ContextUnavailableError:
            user_id = None
        record = await _provider_call(self._store.get_task(
            application_id=self._application.name, job_id=handle._payload_id, user_id=user_id,
        ))
        if record is None or record.task_name != handle.task_name:
            raise TaskRuntimeError("task job was not found")
        return record

    async def _read_outcome(self, handle: TaskHandle) -> tuple[str, Any]:
        """Bridge persisted task state to the existing continuation machinery."""

        return _outcome(await self._handle_record(handle))

    async def _read_provider_outcome(self, payload_id: str) -> tuple[str, Any] | None:
        """Reconcile opaque task IDs after application or worker restarts."""

        record = await self._store.get_task(application_id=self._application.name, job_id=payload_id)
        return None if record is None else _outcome(record)

    async def cancel(self, handle: TaskHandle) -> bool:
        """Commit cancellation and revoke worker leases before notifying waiters."""

        record = await self._handle_record(handle)
        changed = await self._cancel_record(record)
        if changed:
            await self._publish_outcome(record.job_id, ("failed", "task_cancelled"))
        return changed

    async def _cancel_record(self, record: TaskRecord) -> bool:
        """Apply cancellation using the stored application's owner predicates."""

        try:
            changed = await _provider_call(self._store.cancel_task(
                application_id=record.application_id, user_id=record.user_id,
                job_id=record.job_id, now=time.time(),
            ))
        except Exception:
            _audit("cancel", record.trigger, "failed")
            raise
        _audit("cancel", record.trigger, "committed" if changed else "unchanged")
        return changed

    async def _cancel_payload_job(self, payload_id: str) -> bool:
        """Recover an interrupted continuation cancellation through durable state."""

        self._require_ready()
        record = await self._store.get_task(application_id=self._application.name, job_id=payload_id)
        if record is None:
            return False
        return record.status == "cancelled" or await self._cancel_record(record)

    async def _run_worker(self) -> None:
        """Bound concurrent execution and reconcile missed completion callbacks."""

        queues = tuple(sorted({item.queue for item in self._tasks}))
        reconcile_at = 0.0
        while True:
            await self._dynamic_cron.dispatch(time.time())
            capacity = 10 - len(self._active_jobs)
            if capacity > 0:
                records = await self._store.claim_tasks(
                    application_id=self._application.name, queues=queues,
                    now=time.time(), lease_seconds=_LEASE_SECONDS, limit=capacity,
                )
                for record in records:
                    worker = asyncio.create_task(self._run_attempt(record))
                    self._active_jobs.add(worker)
                    worker.add_done_callback(self._attempt_done)
            # Completion callbacks handle the common case; this bounded recovery
            # sweep must not query every continuation on each queue poll.
            if time.monotonic() >= reconcile_at:
                await self.reconcile_continuations()
                reconcile_at = time.monotonic() + 5.0
            await asyncio.sleep(0.1)

    def _attempt_done(self, worker: asyncio.Task[Any]) -> None:
        """Consume background failures while durable leases allow later recovery."""

        self._active_jobs.discard(worker)
        if not worker.cancelled() and worker.exception() is not None:
            _audit("execute", "agent", "failed")

    async def _run_attempt(self, record: TaskRecord) -> None:
        """Execute one attempt while renewing its fenced provider lease."""

        current = asyncio.current_task()
        heartbeat = asyncio.create_task(self._renew(record, current))
        try:
            await self._execute_record(record)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _renew(self, record: TaskRecord, worker: Any) -> None:
        """Stop authored execution when its lease expires or cancellation wins."""

        try:
            while True:
                await asyncio.sleep(_LEASE_SECONDS / 3)
                owned = await self._store.renew_task_lease(
                    application_id=record.application_id, job_id=record.job_id,
                    lease_token=record.lease_token, now=time.time(), lease_seconds=_LEASE_SECONDS,
                )
                if not owned:
                    worker.cancel()
                    return
        except Exception:
            # Uncertain lease renewal cannot grant continued authority to commit.
            worker.cancel()

    async def _execute_record(self, record: TaskRecord) -> None:
        """Restore invocation capabilities and publish only fenced terminal results."""

        compiled = self._task_by_name.get(record.task_name)
        try:
            if compiled is None:
                raise TaskRuntimeError("task target is unavailable")
            snapshot = _record_snapshot(record, self._application.name)
            result = await self._call_authored(
                compiled, safe_task_arguments(record.arguments),
                snapshot,
                _validated_agent_permissions(record.agent_permissions),
                payload_id=record.job_id, trigger=record.trigger,
            )
            result = safe_task_result(result)
        except Exception:
            await self._failed_attempt(record)
            return
        if await self._finish(record, status="completed", result=result):
            await self._publish_outcome(record.job_id, ("completed", result))
            _audit("execute", record.trigger, "committed")

    async def _failed_attempt(self, record: TaskRecord) -> None:
        """Persist retry time or terminal failure without exception payloads."""

        retry = record.attempt <= record.max_retries
        committed = await self._finish(
            record, status="pending" if retry else "failed",
            failure_code=None if retry else "task_failed",
            retry_at=time.time() + min(60, 2 ** min(record.attempt, 6)) if retry else None,
        )
        if committed and not retry:
            await self._publish_outcome(record.job_id, ("failed", "task_failed"))
        _audit("execute", record.trigger, "failed")

    async def _finish(self, record: TaskRecord, **outcome: Any) -> bool:
        """Accept outcome writes only while the exact claimed attempt owns its lease."""

        return await self._store.finish_task(
            application_id=record.application_id, job_id=record.job_id,
            lease_token=record.lease_token, now=time.time(), **outcome,
        )

    def _cron_task_record(self, compiled: Any, record: CronRecord) -> TaskRecord:
        """Create fresh owner identity and empty grants for each scheduled occurrence."""

        occurrence = f"{record.schedule_id}:{record.next_run_at}"
        snapshot = {
            "framework": self._application.framework, "agent_name": self._application.name,
            "invocation_id": occurrence, "session_id": occurrence,
            "user_id": record.user_id, "metadata": {},
        }
        return self._task_record(
            compiled, record.arguments, snapshot, (), trigger="cron",
            key=occurrence, scheduled_at=record.next_run_at,
        )

    async def _register_static_schedules(self) -> None:
        """Reconcile compiled declarations without resetting durable next-run cursors."""

        store = self._application.runtime_capabilities.cron_store
        if store is None:
            return
        expected = {_static_schedule_id(compiled) for compiled in self._crons}
        await self._retire_static_schedules(store, expected)
        for compiled in self._crons:
            await self._register_static_schedule(compiled)

    async def _retire_static_schedules(self, store: Any, expected: set[str]) -> None:
        """Pause removed declarations so old cursors cannot keep producing work."""

        after = None
        while True:
            records = await store.list_crons(
                application_id=self._application.name, user_id=self._automation_user_id,
                after=after, limit=100,
            )
            if not records:
                return
            for record in records:
                if record.key.startswith("static:") and record.schedule_id not in expected and record.status == "active":
                    await store.update_cron(replace(record, status="paused", updated_at=time.time()), expected_revision=record.revision)
            after = records[-1].schedule_id

    async def _register_static_schedule(self, compiled: Any) -> None:
        """Use stable application-owned IDs for static declaration upgrades."""

        store = self._application.runtime_capabilities.cron_store
        now = time.time()
        record = CronRecord(
            schedule_id=_static_schedule_id(compiled),
            application_id=self._application.name, user_id=self._automation_user_id,
            key=f"static:{hashlib.sha256(repr((compiled.name, compiled.task_name)).encode()).hexdigest()}",
            expression=compiled.schedule, task_name=compiled.task_name,
            arguments=safe_task_arguments(compiled.arguments),
            next_run_at=next_occurrence(compiled.schedule, now), created_at=now, updated_at=now,
        )
        try:
            current = await store.create_cron(record)
        except CronStoreConflictError:
            current = await store.get_cron(application_id=record.application_id, user_id=record.user_id, schedule_id=record.schedule_id)
            if current is None:
                raise
        if current.status == "cancelled":
            return
        # Editing arguments or restarting must not skip an already-due occurrence.
        next_run = current.next_run_at if current.expression == record.expression and current.status == "active" else record.next_run_at
        updated = replace(record, revision=current.revision, next_run_at=next_run, created_at=current.created_at)
        if current.expression != updated.expression or current.arguments != updated.arguments or current.status != updated.status:
            await store.update_cron(updated, expected_revision=current.revision)

    async def _stop_worker(self) -> None:
        """Stop claiming before cancelling attempts; leases make interrupted work recoverable."""

        workers = tuple(self._active_jobs)
        if self._worker is not None:
            workers += (self._worker,)
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self._worker = None
        self._active_jobs.clear()

    async def _unwind_start(self) -> None:
        """Release partial bindings and only resources owned by this manager."""

        await self._stop_worker()
        for compiled in self._tasks:
            release_task_runtime(compiled.authored, self)
        if self._manage_storage:
            await self._store.close()

    async def close(self) -> None:
        """Drain the worker before the outer storage lifecycle releases its provider."""

        async with self._lock:
            if self._state == "closed":
                return
            self._state = "closing"
            try:
                await self._unwind_start()
            finally:
                self._state = "closed"


def _outcome(record: TaskRecord) -> tuple[str, Any]:
    """Translate provider states into the existing durable result contract."""

    if record.status == "completed":
        return "completed", record.result
    if record.status == "cancelled":
        return "failed", "task_cancelled"
    if record.status == "failed":
        return "failed", record.failure_code or "task_failed"
    return "pending", None


def _static_schedule_id(compiled: Any) -> str:
    """Give a retargeted declaration a new immutable storage identity."""

    return f"cron_{uuid.uuid5(uuid.NAMESPACE_URL, repr((compiled.name, compiled.task_name))).hex}"


def _record_snapshot(record: TaskRecord, application_id: str) -> Any:
    """Reject inconsistent persisted identities before restoring capabilities."""

    snapshot = _validated_snapshot(record.invocation)
    if record.application_id != application_id:
        raise TaskRuntimeError("task application scope does not match")
    if snapshot is not None and snapshot["user_id"] != record.user_id:
        raise TaskRuntimeError("task owner scope does not match")
    return snapshot


def _audit(operation: str, trigger: str, outcome: str) -> None:
    """Emit stable privacy-safe task signals without naming a storage engine."""

    _AUDIT.info(f"task.{operation}", operation=operation, trigger=trigger, outcome=outcome)


async def _provider_call(call: Any) -> Any:
    """Translate authored-operation failures without leaking database parameters."""

    try:
        return await call
    except TaskStoreConflictError:
        raise TaskRuntimeError("task definition conflicts with stored identity") from None
    except Exception:
        raise TaskRuntimeError("task storage operation failed") from None
