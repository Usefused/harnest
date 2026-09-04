"""Optional Procrastinate ownership for compiler-discovered Harnest tasks."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager, nullcontext
import hashlib
import importlib
import inspect
import json
import os
from types import MappingProxyType
from typing import Any, AsyncIterator, Mapping, Sequence
import uuid

from ._exception_notes import add_exception_note
from .agent_principal import (
    AgentRuntimePrincipal,
    activate_agent_principal,
    active_agent_principal,
    create_agent_principal_binding,
    revoke_agent_principal,
)
from .context import (
    ContextUnavailableError,
    activate_context,
    context,
    create_agent_context,
    revoke_context,
)
from .context_session import invocation_session_context
from .context_agent import LocalAgentRuntime, activate_context_agent
from .continuation import (
    ContinuationConflictError,
    ProviderPendingContinuation,
)
from .cron import CompiledCron
from .credentials import _activate_credential_provider
from .durable import current_native_durable_call
from .external_continuation import (
    ExternalContinuationFailed,
    ExternalContinuationRuntime,
)
from .logging import get_logger
from .runtime_contract import (
    AgentInfo,
    InvocationRequest,
    InvocationResult,
    RuntimeDriver,
    RuntimeEvent,
    SessionMessage,
    SessionRecord,
)
from .session import SessionStore
from .task import (
    CompiledTask,
    TaskCallable,
    TaskHandle,
    TaskUnavailableError,
    bind_task_runtime,
    release_task_runtime,
    safe_task_arguments,
    safe_task_result,
)


_AUDIT = get_logger("task.audit")
_CONTINUATION_PROVIDER = "harnest.task"
_RESULT_CAPABILITY = "task.result"
_RESULT_SCHEMA = "harnest.task.result.v1"
_FAILED = "task_failed"
_CANCELLED = "task_cancelled"
_AUTOMATION_USER_ID = "_harnest_automation"
_WORKER_SHUTDOWN_TIMEOUT_SECONDS = 10
_PAYLOAD_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS harnest_task_payloads (
    payload_id text PRIMARY KEY,
    task_name text NOT NULL,
    arguments jsonb NOT NULL,
    invocation jsonb,
    agent_permissions jsonb,
    trigger text NOT NULL DEFAULT 'agent',
    status text NOT NULL DEFAULT 'pending',
    result jsonb,
    failure_code text,
    completed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
)
"""
_PAYLOAD_MIGRATION_SQL = (
    "ALTER TABLE harnest_task_payloads ADD COLUMN IF NOT EXISTS "
    "status text NOT NULL DEFAULT 'pending'",
    "ALTER TABLE harnest_task_payloads ADD COLUMN IF NOT EXISTS result jsonb",
    "ALTER TABLE harnest_task_payloads ADD COLUMN IF NOT EXISTS failure_code text",
    "ALTER TABLE harnest_task_payloads ADD COLUMN IF NOT EXISTS "
    "completed_at timestamptz",
    "ALTER TABLE harnest_task_payloads ADD COLUMN IF NOT EXISTS "
    "trigger text NOT NULL DEFAULT 'agent'",
    "ALTER TABLE harnest_task_payloads ADD COLUMN IF NOT EXISTS "
    "agent_permissions jsonb",
)
_INSERT_PAYLOAD_SQL = """
INSERT INTO harnest_task_payloads(
    payload_id, task_name, arguments, invocation, agent_permissions, trigger, status
)
VALUES (
    %(payload_id)s, %(task_name)s, %(arguments)s, %(invocation)s,
    %(agent_permissions)s, %(trigger)s, 'pending'
)
ON CONFLICT (payload_id) DO NOTHING
RETURNING payload_id
"""
_GET_NATIVE_JOB_SQL = """
SELECT id, args
FROM procrastinate_jobs
WHERE task_name=%(task_name)s AND queueing_lock=%(queueing_lock)s
  AND args->>'_harnest_payload_id'=%(payload_id)s
ORDER BY id
LIMIT 2
"""
_GET_PAYLOAD_SQL = """
SELECT arguments, invocation, agent_permissions, trigger, status, result, failure_code
FROM harnest_task_payloads
WHERE payload_id=%(payload_id)s AND task_name=%(task_name)s
"""
_GET_PAYLOAD_OUTCOME_SQL = """
SELECT task_name, status, result, failure_code
FROM harnest_task_payloads
WHERE payload_id=%(payload_id)s
"""
_COMPLETE_PAYLOAD_SQL = """
UPDATE harnest_task_payloads
SET status='completed', result=%(result)s, failure_code=NULL,
    arguments='{}'::jsonb, invocation=NULL, agent_permissions=NULL,
    completed_at=now()
WHERE payload_id=%(payload_id)s AND task_name=%(task_name)s
  AND status='pending'
RETURNING payload_id
"""
_FAIL_PAYLOAD_SQL = """
UPDATE harnest_task_payloads
SET status='failed', result=NULL, failure_code=%(failure_code)s,
    arguments='{}'::jsonb, invocation=NULL, agent_permissions=NULL,
    completed_at=now()
WHERE payload_id=%(payload_id)s AND task_name=%(task_name)s
  AND status='pending'
RETURNING payload_id
"""
_CANCEL_PAYLOAD_JOB_SQL = """
WITH target AS (
    SELECT payload.payload_id, payload.task_name, job.id
    FROM harnest_task_payloads AS payload
    JOIN procrastinate_jobs AS job
      ON job.task_name=payload.task_name
     AND job.args->>'_harnest_payload_id'=payload.payload_id
    WHERE payload.payload_id=%(payload_id)s
      AND payload.status='pending'
      AND job.status IN ('todo', 'doing')
    FOR UPDATE OF payload, job
), cancelled AS (
    SELECT target.*,
           procrastinate_cancel_job_v1(target.id, true, false) AS cancelled_id
    FROM target
)
UPDATE harnest_task_payloads AS payload SET
    status='failed', result=NULL, failure_code='task_cancelled',
    arguments='{}'::jsonb, invocation=NULL, agent_permissions=NULL,
    completed_at=now()
FROM cancelled
WHERE payload.payload_id=cancelled.payload_id
  AND cancelled.cancelled_id=cancelled.id
RETURNING payload.payload_id, cancelled.task_name, cancelled.id
"""
_DELETE_PAYLOAD_SQL = """
DELETE FROM harnest_task_payloads WHERE payload_id=%(payload_id)s
"""


class TaskRuntimeError(RuntimeError):
    """The optional task backend failed at a payload-safe runtime boundary."""


class TaskExecutionError(RuntimeError):
    """An authored task failed without exposing its exception message."""


class TaskRuntimeManager:
    """Own one lazy Procrastinate app and its compiler-discovered tasks."""

    def __init__(
        self,
        application: Any,
        *,
        plugin_manager: Any | None = None,
        backend: Any | None = None,
        continuation_runtime: ExternalContinuationRuntime | None = None,
        automation_user_id: str = _AUTOMATION_USER_ID,
        enable_cron: bool = True,
    ) -> None:
        """Retain configuration without importing or connecting to Procrastinate."""

        tasks = tuple(application.tasks)
        if not tasks or any(not isinstance(item, CompiledTask) for item in tasks):
            raise TypeError("task runtime requires compiled tasks")
        self._application = application
        self._tasks = tasks
        self._crons = tuple(application.crons)
        if any(not isinstance(item, CompiledCron) for item in self._crons):
            raise TypeError("task runtime crons must be compiled schedules")
        if not isinstance(enable_cron, bool):
            raise TypeError("enable_cron must be a bool")
        self._enable_cron = enable_cron
        self._plugin_manager = plugin_manager
        self._backend = backend
        self._continuation_runtime: ExternalContinuationRuntime | None = None
        self._invocation_continuations: Any | None = None
        self._application_continuations: Any | None = None
        self._agent_driver: RuntimeDriver | None = None
        self._automation_user_id = _automation_identity(automation_user_id)
        self._app: Any | None = None
        self._native: dict[int, Any] = {}
        self._native_crons: dict[str, Any] = {}
        self._task_by_name = {item.name: item for item in tasks}
        self._worker: asyncio.Task[Any] | None = None
        self._worker_controller: Any | None = None
        self._worker_failure: TaskRuntimeError | None = None
        self._lock = asyncio.Lock()
        self._state = "new"
        if continuation_runtime is not None:
            self.bind_continuations(continuation_runtime)

    def bind_agent_driver(self, driver: RuntimeDriver) -> None:
        """Bind the final pipeline so child sessions cannot bypass capabilities."""

        if not isinstance(driver, RuntimeDriver):
            raise TypeError("task agent driver must implement RuntimeDriver")
        if self._agent_driver is not None and self._agent_driver is not driver:
            raise RuntimeError("task agent driver is already bound")
        self._agent_driver = driver

    @property
    def application(self) -> Any:
        """Expose the compiled application only to the owning runtime driver."""

        return self._application

    def bind_continuations(self, runtime: ExternalContinuationRuntime) -> None:
        """Bind task outcomes to the application-wide continuation authority."""

        if not isinstance(runtime, ExternalContinuationRuntime):
            raise TypeError("task continuations must be ExternalContinuationRuntime")
        if (
            self._continuation_runtime is not None
            and self._continuation_runtime is not runtime
        ):
            raise RuntimeError("task continuation runtime is already bound")
        if self._continuation_runtime is runtime:
            return
        self._continuation_runtime = runtime
        self._invocation_continuations = runtime.invocation_port(
            _CONTINUATION_PROVIDER
        )
        self._application_continuations = runtime.application_port(
            _CONTINUATION_PROVIDER
        )
        self._application_continuations.register_schema(
            _RESULT_SCHEMA, _validate_continuation_result
        )
        runtime.register_cancel_handler(
            _CONTINUATION_PROVIDER, self._cancel_provider_wait
        )

    async def start(self) -> None:
        """Open schema ownership, bind task callables, and launch one worker."""

        if self._state == "started":
            self._require_ready()
            return
        async with self._lock:
            if self._state == "started":
                self._require_ready()
                return
            if self._state != "new":
                raise TaskRuntimeError("task runtime cannot be restarted")
            try:
                await self._start_locked()
            except BaseException as error:
                self._state = "failed"
                await self._unwind_start()
                if isinstance(error, asyncio.CancelledError):
                    raise
                if isinstance(error, TaskRuntimeError):
                    raise
                raise TaskRuntimeError(
                    "task runtime startup failed with "
                    f"{type(error).__name__}"
                ) from None
            self._state = "started"

    async def _start_locked(self) -> None:
        """Construct the backend only after the compiled application needs it."""

        backend = self._backend or _load_procrastinate()
        self._backend = backend
        connector = backend.PsycopgConnector(
            conninfo=_task_database_dsn(self._application)
        )
        self._app = backend.App(connector=connector)
        self._register_native_tasks()
        if self._enable_cron:
            self._register_native_crons()
        await self._app.open_async()
        await _ensure_procrastinate_schema(self._app)
        await self._app.connector.execute_query_async(query=_PAYLOAD_TABLE_SQL)
        # Existing task databases predate result ownership, so additive columns
        # are installed without discarding already queued opaque payloads.
        for query in _PAYLOAD_MIGRATION_SQL:
            await self._app.connector.execute_query_async(query=query)
        for compiled in self._tasks:
            bind_task_runtime(compiled.authored, self)
        options = {
            "install_signal_handlers": False,
            "shutdown_graceful_timeout": _WORKER_SHUTDOWN_TIMEOUT_SECONDS,
        }
        # Procrastinate shields its internal worker loop from task cancellation.
        # Retaining the pinned backend's worker lets the server lifespan request
        # a real stop when startup later fails, such as on an HTTP bind collision.
        self._worker_controller = self._app._worker(**options)
        self._worker = asyncio.create_task(
            self._worker_controller.run(),
            name=f"harnest-tasks-{self._application.name}",
        )
        self._worker.add_done_callback(self._worker_done)

    def _register_native_tasks(self) -> None:
        """Register opaque-payload wrappers so queue logs never contain arguments."""

        if self._app is None:  # pragma: no cover - caller owns construction
            raise RuntimeError("task app is unavailable")
        for compiled in self._tasks:
            execute = self._native_executor(compiled)
            native = self._app.task(
                name=compiled.name,
                queue=compiled.queue,
                retry=compiled.max_retries,
                pass_context=True,
            )(execute)
            self._native[id(compiled.authored)] = native

    def _native_executor(self, compiled: CompiledTask) -> Any:
        """Create a worker entrypoint whose result and failures are sanitized."""

        async def execute(job_context: Any, _harnest_payload_id: str) -> None:
            await self._execute(compiled, _harnest_payload_id, job_context)

        execute.__name__ = compiled.name.rsplit(".", 1)[-1]
        execute.__doc__ = "Execute one compiler-owned opaque Harnest task payload."
        return execute

    def _register_native_crons(self) -> None:
        """Register periodic dispatchers before workers inspect their schedules."""

        if self._app is None:  # pragma: no cover - caller owns construction
            raise RuntimeError("task app is unavailable")
        for compiled in self._crons:
            execute = self._cron_executor(compiled)
            native = self._app.task(
                name=f"{compiled.name}.dispatch",
                queue=compiled.task.queue,
                retry=compiled.task.max_retries,
            )(execute)
            self._app.periodic(
                cron=compiled.schedule,
                periodic_id=compiled.name,
            )(native)
            self._native_crons[compiled.name] = native

    def _cron_executor(self, compiled: CompiledCron) -> Any:
        """Create a payload-free periodic entrypoint for one compiled schedule."""

        async def execute(timestamp: int) -> None:
            await self._enqueue_cron(compiled, timestamp)

        execute.__name__ = f"{compiled.name.rsplit('.', 1)[-1]}_dispatch"
        execute.__doc__ = "Enqueue one compiler-owned Harnest cron occurrence."
        return execute

    async def defer(
        self,
        task_value: TaskCallable[Any],
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str | None,
        schedule_in: float | None,
    ) -> TaskHandle:
        """Persist private arguments before committing an opaque native job."""

        await self.start()
        self._require_ready()
        compiled = self._compiled_for(task_value)
        snapshot = _capture_invocation()
        agent_permissions = _capture_agent_permissions()
        trigger = "agent" if snapshot is not None else "user"
        if idempotency_key is None:
            idempotency_key = _native_idempotency_key(
                self._application, compiled, arguments, snapshot
            )
        return await self._defer_compiled(
            compiled,
            arguments,
            snapshot,
            agent_permissions,
            trigger=trigger,
            idempotency_key=idempotency_key,
            schedule_in=schedule_in,
        )

    async def _enqueue_cron(self, cron: CompiledCron, timestamp: int) -> None:
        """Turn one native periodic tick into an idempotent private task job."""

        if (
            not isinstance(timestamp, int)
            or isinstance(timestamp, bool)
            or timestamp < 0
        ):
            _cron_audit("enqueue", cron.name, "failed")
            raise TaskRuntimeError("cron timestamp must be a non-negative integer")
        try:
            await self._defer_compiled(
                cron.task,
                safe_task_arguments(cron.arguments),
                None,
                None,
                trigger="cron",
                idempotency_key=f"{cron.name}:{timestamp}",
                schedule_in=None,
            )
        except BaseException:
            _cron_audit("enqueue", cron.name, "failed")
            raise
        _cron_audit("enqueue", cron.name, "committed")

    async def _defer_compiled(
        self,
        compiled: CompiledTask,
        arguments: Mapping[str, Any],
        snapshot: Mapping[str, Any] | None,
        agent_permissions: frozenset[str] | None = None,
        *,
        trigger: str,
        idempotency_key: str | None,
        schedule_in: float | None,
    ) -> TaskHandle:
        """Commit a resolved task through the one private payload transaction."""

        queueing_lock = _queueing_lock(compiled.name, idempotency_key)
        payload_id = _payload_id(compiled.name, queueing_lock)
        try:
            job_id, committed_payload_id = await self._commit_job(
                compiled,
                compiled.authored,
                payload_id,
                arguments,
                snapshot,
                agent_permissions,
                trigger=trigger,
                queueing_lock=queueing_lock,
                schedule_in=schedule_in,
            )
        except BaseException as error:
            try:
                existing = await self._existing_job(error, compiled, queueing_lock)
            except Exception:
                _audit("defer", trigger, "failed")
                raise
            if existing is not None:
                await self._delete_payload_quietly(payload_id)
                _audit("defer", trigger, "committed")
                return self._handle(compiled, existing[0], existing[1], trigger)
            # An I/O failure may happen after PostgreSQL committed the native
            # job. Retaining the opaque payload avoids converting uncertainty
            # into guaranteed data loss; later retention cleanup can prune it.
            _audit("defer", trigger, "failed")
            if isinstance(error, asyncio.CancelledError):
                raise
            raise TaskRuntimeError(
                f"task defer failed with {type(error).__name__}"
            ) from None
        _audit("defer", trigger, "committed")
        return self._handle(compiled, job_id, committed_payload_id, trigger)

    async def _commit_job(
        self,
        compiled: CompiledTask,
        task_value: TaskCallable[Any],
        payload_id: str,
        arguments: Mapping[str, Any],
        snapshot: Mapping[str, Any] | None,
        agent_permissions: frozenset[str] | None,
        *,
        trigger: str,
        queueing_lock: str | None,
        schedule_in: float | None,
    ) -> tuple[int, str]:
        """Commit private payload and opaque queue row in one transaction."""

        # Procrastinate 3.9 explicitly supports external Psycopg connections.
        # Sharing one transaction prevents jobs without payloads and vice versa.
        async with self._app.connector.pool.connection() as connection:
            inserted = await self._insert_payload(
                compiled,
                payload_id,
                arguments,
                snapshot,
                agent_permissions,
                trigger=trigger,
                connection=connection,
            )
            if not inserted:
                return (
                    await self._job_for_payload(
                        compiled, payload_id, queueing_lock, connection
                    ),
                    payload_id,
                )
            job_id = await self._defer_native(
                task_value,
                payload_id,
                queueing_lock=queueing_lock,
                schedule_in=schedule_in,
                connection=connection,
            )
            return job_id, payload_id

    async def _defer_native(
        self,
        task_value: TaskCallable[Any],
        payload_id: str,
        *,
        queueing_lock: str | None,
        schedule_in: float | None,
        connection: Any,
    ) -> int:
        """Configure scheduling once before the native durable insert."""

        options: dict[str, Any] = {"connection": connection}
        if queueing_lock is not None:
            options["queueing_lock"] = queueing_lock
        if schedule_in is not None:
            options["schedule_in"] = {"seconds": schedule_in}
        native = self._native[id(task_value)]
        return await native.configure(**options).defer_async(
            _harnest_payload_id=payload_id
        )

    async def _job_for_payload(
        self,
        compiled: CompiledTask,
        payload_id: str,
        queueing_lock: str | None,
        connection: Any,
    ) -> int:
        """Resolve one transactionally committed idempotent native job."""

        if queueing_lock is None:  # pragma: no cover - random ids cannot conflict
            raise TaskRuntimeError("non-idempotent task payload already exists")
        rows = await self._app.connector.execute_query_all_async_with_connection(
            connection,
            query=_GET_NATIVE_JOB_SQL,
            task_name=compiled.name,
            queueing_lock=queueing_lock,
            payload_id=payload_id,
        )
        if len(rows) != 1:
            raise TaskRuntimeError("idempotent task job lookup returned an invalid result")
        return int(rows[0]["id"])

    async def _existing_job(
        self,
        error: BaseException,
        compiled: CompiledTask,
        queueing_lock: str | None,
    ) -> tuple[int, str] | None:
        """Resolve only the single row protected by a hashed idempotency lock."""

        already = getattr(
            getattr(self._backend, "exceptions", None), "AlreadyEnqueued", ()
        )
        if queueing_lock is None or not isinstance(error, already):
            return None
        jobs = tuple(
            await self._app.job_manager.list_jobs_async(
                task=compiled.name, queueing_lock=queueing_lock
            )
        )
        if len(jobs) != 1:
            raise TaskRuntimeError("idempotent task lookup returned an invalid result")
        payload_id = jobs[0].task_kwargs.get("_harnest_payload_id")
        if not isinstance(payload_id, str) or not payload_id:
            raise TaskRuntimeError("idempotent task payload reference is invalid")
        return int(jobs[0].id), payload_id

    async def status(self, handle: TaskHandle) -> str:
        """Return one native status without selecting or filtering jobs in memory."""

        self._require_handle(handle)
        self._require_ready()
        try:
            status = await self._app.job_manager.get_job_status_async(int(handle.id))
        except Exception as error:
            raise TaskRuntimeError(
                f"task status failed with {type(error).__name__}"
            ) from None
        value = getattr(status, "value", status)
        return str(value)

    async def result(self, handle: TaskHandle) -> Any:
        """Return persisted output or enter the shared native continuation path."""

        self._require_handle(handle)
        self._require_ready()
        outcome = await self._read_outcome(handle)
        if outcome[0] != "pending":
            return _task_result(outcome)
        if current_native_durable_call() is None:
            raise TaskUnavailableError(
                "unfinished task result requires an async @tool(durable=True)"
            )
        if self._invocation_continuations is None:
            raise TaskUnavailableError(
                "unfinished task result requires a HarnestStore checkpointer"
            )
        suspended = await self._invocation_continuations.suspend(
            handle._payload_id,
            capability=_RESULT_CAPABILITY,
            schema_id=_RESULT_SCHEMA,
            validate=_validate_continuation_result,
        )
        # The worker may commit between the first read and durable registration.
        # Re-reading after registration closes that race without polling memory.
        outcome = await self._read_outcome(handle)
        if outcome[0] != "pending":
            await self._publish_outcome(handle._payload_id, outcome)
        try:
            return await suspended.result()
        except ExternalContinuationFailed as error:
            raise TaskExecutionError(
                f"task result failed: {error.code}"
            ) from None

    async def cancel(self, handle: TaskHandle) -> bool:
        """Request cancellation for queued or currently executing async work."""

        self._require_handle(handle)
        self._require_ready()
        try:
            cancelled = await self._app.job_manager.cancel_job_by_id_async(
                int(handle.id), abort=True
            )
        except Exception as error:
            _audit("cancel", handle._trigger, "failed")
            raise TaskRuntimeError(
                f"task cancellation failed with {type(error).__name__}"
            ) from None
        _audit("cancel", handle._trigger, "committed" if cancelled else "unchanged")
        if cancelled:
            outcome = await self._persist_failure(
                handle.task_name, handle._payload_id, _CANCELLED
            )
            await self._publish_outcome(handle._payload_id, outcome)
        return bool(cancelled)

    async def _cancel_provider_wait(
        self, pending: ProviderPendingContinuation
    ) -> bool:
        """Stop one awaited native task before cancelling its durable run."""

        record = pending.record
        valid = (
            record.provider == _CONTINUATION_PROVIDER
            and record.capability == _RESULT_CAPABILITY
            and record.schema_id == _RESULT_SCHEMA
            and record.status == "pending"
        )
        if not valid or self._application_continuations is None:
            return False
        stopped = await self._cancel_payload_job(pending.external_id)
        if not stopped:
            return False
        try:
            await self._application_continuations.cancel(record, _CANCELLED)
        except Exception as error:
            # The native transaction already made cancellation durable. Startup
            # reconciliation will converge a callback race or store outage.
            _audit("cancel", "agent", "failed")
            raise TaskRuntimeError(
                "durable task cancellation failed with "
                f"{type(error).__name__}"
            ) from None
        _audit("cancel", "agent", "committed")
        return True

    async def _cancel_payload_job(self, payload_id: str) -> bool:
        """Atomically stop native execution and erase its private task payload."""

        self._require_ready()
        try:
            rows = await self._app.connector.execute_query_all_async(
                query=_CANCEL_PAYLOAD_JOB_SQL,
                payload_id=payload_id,
            )
        except Exception as error:
            raise TaskRuntimeError(
                f"task cancellation failed with {type(error).__name__}"
            ) from None
        if len(rows) > 1:
            raise TaskRuntimeError("task cancellation matched multiple native jobs")
        if rows:
            return True
        # A retry after the queue transaction committed must still be able to
        # finish the separate checkpoint-store cancellation step.
        outcome = await self._read_provider_outcome(payload_id)
        return outcome == ("failed", _CANCELLED)

    async def reconcile_continuations(self) -> None:
        """Converge retained task outcomes after callbacks or replicas restart."""

        port = self._application_continuations
        if port is None:
            return
        after: str | None = None
        while True:
            try:
                pending = tuple(await port.list_pending(after=after, limit=100))
            except Exception:
                _audit("result.reconcile", "agent", "failed")
                return
            for item in pending:
                await self._reconcile_continuation(item)
            if len(pending) < 100:
                return
            after = pending[-1].record.continuation_id

    async def _reconcile_continuation(
        self, pending: ProviderPendingContinuation
    ) -> None:
        """Publish one retained terminal row through its exact provider wait."""

        try:
            outcome = await self._read_provider_outcome(pending.external_id)
            if outcome is None or outcome[0] == "pending":
                return
            if outcome == ("failed", _CANCELLED):
                await self._application_continuations.cancel(
                    pending.record, _CANCELLED
                )
            else:
                await self._publish_outcome(pending.external_id, outcome)
        except (ContinuationConflictError, KeyError):
            # Another replica won the same provider/run compare-and-swap.
            return
        except Exception:
            _audit("result.reconcile", "agent", "failed")

    async def _read_provider_outcome(
        self, payload_id: str
    ) -> tuple[str, Any] | None:
        """Read a provider-indexed outcome without requiring an in-memory handle."""

        try:
            row = await self._app.connector.execute_query_one_async(
                query=_GET_PAYLOAD_OUTCOME_SQL,
                payload_id=payload_id,
            )
        except Exception as error:
            # Connector implementations use lookup failures for an absent row.
            if _is_no_result(self._backend, error):
                return None
            raise TaskRuntimeError(
                f"task result read failed with {type(error).__name__}"
            ) from None
        return _validated_outcome(row)

    async def _execute(
        self, compiled: CompiledTask, payload_id: str, job_context: Any
    ) -> None:
        """Restore safe invocation capabilities around one authored callable."""

        arguments, snapshot, agent_permissions, trigger = await self._get_payload(
            compiled, payload_id
        )
        try:
            result = await self._call_authored(
                compiled,
                arguments,
                snapshot,
                agent_permissions,
                payload_id=payload_id,
                trigger=trigger,
            )
        except asyncio.CancelledError:
            _audit("execute", trigger, "cancelled")
            raise
        except Exception as error:
            if _is_final_attempt(job_context, compiled.max_retries):
                outcome = await self._persist_failure(
                    compiled.name, payload_id, _FAILED
                )
                await self._publish_outcome(payload_id, outcome)
            _audit("execute", trigger, "failed")
            raise TaskExecutionError(
                f"task execution failed with {type(error).__name__}"
            ) from None
        outcome = await self._persist_result(compiled.name, payload_id, result)
        # Continuation delivery is recoverable from the retained task row, so a
        # callback outage must not rerun an already-successful authored effect.
        await self._publish_outcome(payload_id, outcome)
        _audit("execute", trigger, "committed")

    async def _call_authored(
        self,
        compiled: CompiledTask,
        arguments: Mapping[str, Any],
        snapshot: Mapping[str, Any] | None,
        agent_permissions: frozenset[str] | None = None,
        *,
        payload_id: str,
        trigger: str = "agent",
    ) -> Any:
        """Run inline or inside a reconstructed managed task invocation."""

        agent_scope = self._agent_scope(snapshot, payload_id, trigger)
        with _task_agent_principal_scope(agent_permissions):
            if snapshot is None:
                with agent_scope:
                    return await _resolve_task_call(compiled.function, arguments)
            active = self._agent_context(snapshot)
            session_store = self._session_store()
            try:
                async with invocation_session_context(
                    session_store,
                    framework=active.framework,
                    user_id=active.user_id,
                    session_id=active.session_id,
                    invocation_id=active.invocation_id,
                    trigger="agent",
                ):
                    with (
                        activate_context(active),
                        self._credential_scope(),
                        agent_scope,
                    ):
                        return await _resolve_task_call(compiled.function, arguments)
            finally:
                revoke_context(active)

    def _agent_scope(
        self,
        snapshot: Mapping[str, Any] | None,
        payload_id: str,
        trigger: str,
    ) -> Any:
        """Expose child invocation only when the final runtime owns this worker."""

        if self._agent_driver is None:
            return nullcontext()
        user_id = (
            self._automation_user_id if snapshot is None else snapshot["user_id"]
        )
        runtime = LocalAgentRuntime(
            self._agent_driver,
            user_id=user_id,
            transport="task",
            trigger=trigger,
            identity_namespace=payload_id,
            metadata=None if snapshot is None else snapshot["metadata"],
        )
        return activate_context_agent(runtime)

    def _agent_context(self, snapshot: Mapping[str, Any]) -> Any:
        """Reconstruct stable capabilities without copying secret credentials."""

        capabilities = self._application.runtime_capabilities
        resources = {item.name: item.value for item in capabilities.context_values}
        bindings = (
            None
            if self._plugin_manager is None
            else self._plugin_manager.invocation_bindings()
        )
        return create_agent_context(
            framework=snapshot["framework"],
            agent_name=snapshot["agent_name"],
            invocation_id=snapshot["invocation_id"],
            user_id=snapshot["user_id"],
            session_id=snapshot["session_id"],
            metadata=snapshot["metadata"],
            resources=resources,
            asset_stores=capabilities.asset_stores,
            custom_stores=capabilities.custom_stores,
            skill_registry=capabilities.skill_registry,
            sandbox_registry=capabilities.sandbox_registry,
            plugin_bindings=bindings,
        )

    def _session_store(self) -> SessionStore | None:
        """Use only a portable store that can acquire the task's session lease."""

        candidate = self._application.runtime_capabilities.session_store
        return candidate if isinstance(candidate, SessionStore) else None

    def _credential_scope(self) -> Any:
        """Bind the provider at execution time so no credential enters task JSON."""

        provider = self._application.runtime_capabilities.credential_provider
        return (
            nullcontext()
            if provider is None
            else _activate_credential_provider(provider)
        )

    async def _insert_payload(
        self,
        compiled: CompiledTask,
        payload_id: str,
        arguments: Mapping[str, Any],
        snapshot: Mapping[str, Any] | None,
        agent_permissions: frozenset[str] | None,
        *,
        trigger: str,
        connection: Any,
    ) -> bool:
        """Store private content outside Procrastinate's argument-bearing logs."""

        try:
            rows = await self._app.connector.execute_query_all_async_with_connection(
                connection,
                query=_INSERT_PAYLOAD_SQL,
                payload_id=payload_id,
                task_name=compiled.name,
                arguments=dict(arguments),
                invocation=None if snapshot is None else dict(snapshot),
                agent_permissions=(
                    None if agent_permissions is None else sorted(agent_permissions)
                ),
                trigger=trigger,
            )
        except Exception as error:
            raise TaskRuntimeError(
                f"task payload persistence failed with {type(error).__name__}"
            ) from None
        if len(rows) > 1:
            raise TaskRuntimeError("task payload insert returned an invalid result")
        return bool(rows)

    async def _get_payload(
        self, compiled: CompiledTask, payload_id: str
    ) -> tuple[
        dict[str, Any], Mapping[str, Any] | None, frozenset[str] | None, str
    ]:
        """Read one task-owned payload with its stable task-name predicate."""

        try:
            row = await self._app.connector.execute_query_one_async(
                query=_GET_PAYLOAD_SQL,
                payload_id=payload_id,
                task_name=compiled.name,
            )
            arguments = safe_task_arguments(row["arguments"])
            snapshot = _validated_snapshot(row.get("invocation"))
            agent_permissions = _validated_agent_permissions(
                row.get("agent_permissions")
            )
            trigger = _validated_trigger(row.get("trigger", "agent"))
        except Exception as error:
            raise TaskRuntimeError(
                f"task payload read failed with {type(error).__name__}"
            ) from None
        return arguments, snapshot, agent_permissions, trigger

    async def _read_outcome(self, handle: TaskHandle) -> tuple[str, Any]:
        """Read one task result through its payload and compiled-name predicates."""

        try:
            row = await self._app.connector.execute_query_one_async(
                query=_GET_PAYLOAD_SQL,
                payload_id=handle._payload_id,
                task_name=handle.task_name,
            )
            return _validated_outcome(row)
        except Exception as error:
            if isinstance(error, TaskExecutionError):
                raise
            raise TaskRuntimeError(
                f"task result read failed with {type(error).__name__}"
            ) from None

    async def _persist_result(
        self, task_name: str, payload_id: str, result: Any
    ) -> tuple[str, Any]:
        """Commit one JSON-safe result before acknowledging worker success."""

        envelope = {"value": safe_task_result(result)}
        await self._update_outcome(
            _COMPLETE_PAYLOAD_SQL,
            payload_id=payload_id,
            task_name=task_name,
            result=envelope,
        )
        return "completed", envelope["value"]

    async def _persist_failure(
        self, task_name: str, payload_id: str, failure_code: str
    ) -> tuple[str, Any]:
        """Commit a payload-free terminal code before notifying a waiter."""

        await self._update_outcome(
            _FAIL_PAYLOAD_SQL,
            payload_id=payload_id,
            task_name=task_name,
            failure_code=failure_code,
        )
        return "failed", failure_code

    async def _update_outcome(self, query: str, **values: Any) -> None:
        """Apply one pending-only outcome transition with a database predicate."""

        try:
            rows = await self._app.connector.execute_query_all_async(
                query=query, **values
            )
        except Exception as error:
            raise TaskRuntimeError(
                f"task result persistence failed with {type(error).__name__}"
            ) from None
        if len(rows) != 1:
            raise TaskRuntimeError("task result transition did not own a pending row")

    async def _publish_outcome(
        self, payload_id: str, outcome: tuple[str, Any]
    ) -> None:
        """Best-effort publish a persisted outcome to a registered durable wait."""

        port = self._application_continuations
        if port is None or outcome[0] == "pending":
            return
        try:
            if outcome[0] == "completed":
                await port.complete(payload_id, {"value": outcome[1]})
            else:
                await port.fail(payload_id, outcome[1])
        except (ContinuationConflictError, KeyError):
            # No waiter is normal when work beats result registration; a later
            # result() call re-reads this row and publishes after registering.
            return
        except Exception:
            _audit("result.notify", "agent", "failed")

    async def _delete_payload_quietly(self, payload_id: str) -> None:
        """Best-effort rollback keeps the original queue failure authoritative."""

        try:
            await self._app.connector.execute_query_async(
                query=_DELETE_PAYLOAD_SQL, payload_id=payload_id
            )
        except Exception:
            return

    def _compiled_for(self, task_value: TaskCallable[Any]) -> CompiledTask:
        """Resolve only a callable bound by this compiler-owned runtime."""

        for compiled in self._tasks:
            if compiled.authored is task_value:
                return compiled
        raise TaskRuntimeError("task callable does not belong to this runtime")

    def _handle(
        self,
        compiled: CompiledTask,
        job_id: int,
        payload_id: str,
        trigger: str,
    ) -> TaskHandle:
        """Issue an opaque handle tied to this runtime and private payload."""

        return TaskHandle(str(job_id), compiled.name, self, payload_id, trigger)

    def _require_handle(self, handle: TaskHandle) -> None:
        """Reject forged or cross-runtime handles before native database access."""

        if not isinstance(handle, TaskHandle) or handle._runtime is not self:
            raise TaskRuntimeError("task handle does not belong to this runtime")
        if handle.task_name not in self._task_by_name:
            raise TaskRuntimeError("task handle references an unknown task")

    def _require_ready(self) -> None:
        """Surface worker termination before accepting more task operations."""

        if self._state != "started" or self._app is None:
            raise TaskRuntimeError("task runtime is not started")
        if self._worker_failure is not None:
            raise self._worker_failure

    def _worker_done(self, worker: asyncio.Task[Any]) -> None:
        """Consume terminal worker state so background failures are never lost."""

        if worker.cancelled() or self._state in {"closing", "closed"}:
            return
        error = worker.exception()
        suffix = "unexpectedly" if error is None else f"with {type(error).__name__}"
        self._worker_failure = TaskRuntimeError(f"task worker stopped {suffix}")
        _audit("worker", "agent", "failed")

    async def close(self) -> None:
        """Stop queue execution before releasing authored runtime capabilities."""

        failure: BaseException | None = None
        async with self._lock:
            if self._state == "closed":
                return
            self._state = "closing"
            failure = await _cleanup_failure(self._stop_worker)
            for compiled in self._tasks:
                release_task_runtime(compiled.authored, self)
            app = self._app
            self._app = None
            if app is not None:
                try:
                    await app.close_async()
                except BaseException as error:
                    if failure is None:
                        failure = error
                    else:
                        add_exception_note(
                            failure,
                            "task connector cleanup also failed with "
                            f"{type(error).__name__}",
                        )
            self._state = "closed"
        if failure is not None:
            if isinstance(failure, asyncio.CancelledError):
                raise failure
            raise TaskRuntimeError(
                "task runtime cleanup failed with "
                f"{type(failure).__name__}"
            ) from None

    async def _unwind_start(self) -> None:
        """Release every task binding and connector acquired before startup failed."""

        await _cleanup_failure(self._stop_worker)
        for compiled in self._tasks:
            release_task_runtime(compiled.authored, self)
        app = self._app
        self._app = None
        if app is not None:
            try:
                await app.close_async()
            except Exception:
                return

    async def _stop_worker(self) -> None:
        """Stop the owned native loop before closing its PostgreSQL connector."""

        worker = self._worker
        controller = self._worker_controller
        self._worker = None
        self._worker_controller = None
        if worker is None:
            return
        if controller is None:
            worker.cancel()
        else:
            controller.stop()
        await _await_cancelled(worker)


class TaskRuntimeDriver(RuntimeDriver):
    """Start queue workers after inner capabilities and stop them before teardown."""

    def __init__(self, driver: RuntimeDriver, manager: TaskRuntimeManager) -> None:
        """Retain explicit ownership without importing the optional backend."""

        if not isinstance(manager, TaskRuntimeManager):
            raise TypeError("manager must be TaskRuntimeManager")
        self._driver = driver
        self._manager = manager
        # Tasks must re-enter this outer wrapper so storage, plugins, context,
        # credentials, lifecycle hooks, and checkpoints remain in force.
        manager.bind_agent_driver(self)
        self._lock = asyncio.Lock()
        self._state = "new"
        continuations = getattr(driver, "external_continuations", None)
        self._owns_continuations = continuations is None
        if continuations is None:
            continuations = _task_continuation_runtime(manager.application)
        self.external_continuations = continuations
        if continuations is not None:
            manager.bind_continuations(continuations)
            # Callbacks must re-enter the full task-aware pipeline so replayed
            # defer calls recover the same queue job before reading its result.
            continuations.bind_driver(self)

    @property
    def info(self) -> AgentInfo:
        return self._driver.info

    async def start(self) -> None:
        """Start framework capabilities before tasks can execute against them."""

        if self._state == "started":
            return
        async with self._lock:
            if self._state == "started":
                return
            if self._state != "new":
                raise TaskRuntimeError("task runtime driver cannot be restarted")
            try:
                starter = getattr(self._driver, "start", None)
                if callable(starter):
                    await starter()
                await self._manager.start()
                # Reconciliation may resume through this outer driver, so mark
                # it started before provider callbacks re-enter the pipeline.
                self._state = "started"
                await self._manager.reconcile_continuations()
            except BaseException as failure:
                self._state = "failed"
                cleanup = await _cleanup_failure(self._driver.close)
                if cleanup is not None:
                    add_exception_note(
                        failure,
                        "runtime cleanup also failed with "
                        f"{type(cleanup).__name__}"
                    )
                raise

    async def cancel_durable_task(
        self, *, response_id: str, user_id: str, session_id: str
    ) -> bool:
        """Cancel one awaited task through shared continuation ownership."""

        await self.start()
        if self.external_continuations is None:
            return False
        return await self.external_continuations.cancel_task_wait(
            response_id=response_id,
            user_id=user_id,
            session_id=session_id,
        )

    async def create_session(
        self, *, session_id: str, user_id: str, state: Mapping[str, Any]
    ) -> SessionRecord:
        await self.start()
        return await self._driver.create_session(
            session_id=session_id, user_id=user_id, state=state
        )

    async def get_session(
        self, *, session_id: str, user_id: str
    ) -> SessionRecord | None:
        await self.start()
        return await self._driver.get_session(session_id=session_id, user_id=user_id)

    async def list_sessions(
        self,
        *,
        user_id: str,
        after: str | None = None,
        limit: int | None = None,
    ) -> Sequence[SessionRecord]:
        """Forward one bounded page after queue startup succeeds."""

        await self.start()
        if after is None and limit is None:
            return await self._driver.list_sessions(user_id=user_id)
        return await self._driver.list_sessions(
            user_id=user_id, after=after, limit=limit
        )

    async def get_session_messages(
        self, *, session_id: str, user_id: str
    ) -> Sequence[SessionMessage] | None:
        """Read framework messages only after runtime startup."""

        await self.start()
        return await self._driver.get_session_messages(
            session_id=session_id, user_id=user_id
        )

    async def update_session(
        self,
        *,
        session_id: str,
        user_id: str,
        state_delta: Mapping[str, Any],
    ) -> SessionRecord | None:
        await self.start()
        return await self._driver.update_session(
            session_id=session_id, user_id=user_id, state_delta=state_delta
        )

    async def delete_session(self, *, session_id: str, user_id: str) -> bool:
        await self.start()
        return await self._driver.delete_session(
            session_id=session_id, user_id=user_id
        )

    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        await self.start()
        return await self._driver.invoke(request)

    async def stream(
        self, request: InvocationRequest
    ) -> AsyncIterator[RuntimeEvent]:
        await self.start()
        async for event in self._driver.stream(request):
            yield event

    async def close(self) -> None:
        """Drain tasks before credentials, plugins, and storage are released."""

        async with self._lock:
            if self._state == "closed":
                return
            self._state = "closed"
        failure = await _cleanup_failure(self._manager.close)
        if self._owns_continuations and self.external_continuations is not None:
            continuation_failure = await _cleanup_failure(
                self.external_continuations.close
            )
            if failure is None:
                failure = continuation_failure
            elif continuation_failure is not None:
                add_exception_note(
                    failure,
                    "runtime cleanup also failed with "
                    f"{type(continuation_failure).__name__}"
                )
        inner_failure = await _cleanup_failure(self._driver.close)
        if failure is None:
            failure = inner_failure
        elif inner_failure is not None:
            add_exception_note(
                failure,
                "runtime cleanup also failed with "
                f"{type(inner_failure).__name__}"
            )
        if failure is not None:
            raise failure


async def _ensure_procrastinate_schema(app: Any) -> None:
    """Apply the native schema once, tolerating a concurrent replica winner."""

    if await app.check_connection_async():
        return
    try:
        await app.schema_manager.apply_schema_async()
    except Exception:
        # Multiple replicas may observe an empty database simultaneously. Only
        # accept a failed migration when the winner left a complete schema.
        if not await app.check_connection_async():
            raise


def _task_database_dsn(application: Any) -> str:
    """Resolve an explicit override or one unambiguous shared Postgres store."""

    configured = os.environ.get("HARNEST_TASK_DATABASE_URL")
    if configured is not None:
        if not configured.strip():
            raise TaskRuntimeError("HARNEST_TASK_DATABASE_URL cannot be empty")
        return configured
    from .store_postgres import PostgresStore

    candidates = tuple(
        resource
        for resource in (
            application.runtime_capabilities.storage_registry.owned_resources()
        )
        if isinstance(resource, PostgresStore)
    )
    if len(candidates) == 1:
        return candidates[0]._task_database_dsn()
    if not candidates:
        raise TaskRuntimeError(
            "compiled tasks require HARNEST_TASK_DATABASE_URL or a PostgresStore"
        )
    raise TaskRuntimeError(
        "multiple PostgresStore resources require HARNEST_TASK_DATABASE_URL"
    )


def _automation_identity(value: Any) -> str:
    """Validate the non-secret owner used by scheduler-triggered task sessions."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("task automation_user_id must be non-empty text")
    return value


def _task_continuation_runtime(application: Any) -> ExternalContinuationRuntime | None:
    """Create shared continuation ownership when Harnest owns checkpoints."""

    from .checkpoint import HarnestStore

    store = application.runtime_capabilities.checkpointer
    if not isinstance(store, HarnestStore):
        return None
    return ExternalContinuationRuntime(store, application_id=application.name)


def _capture_invocation() -> Mapping[str, Any] | None:
    """Capture identity and public metadata without credentials or resource values."""

    try:
        active = context.current()
    except ContextUnavailableError:
        return None
    return MappingProxyType(
        {
            "framework": active.framework,
            "agent_name": active.agent_name,
            "invocation_id": active.invocation_id,
            "user_id": active.user_id,
            "session_id": active.session_id,
            "metadata": safe_task_arguments(active.metadata),
        }
    )


def _capture_agent_permissions() -> frozenset[str] | None:
    """Capture only non-secret grants needed to constrain durable child work."""

    principal = active_agent_principal()
    return None if principal is None else principal.permissions


def _validated_agent_permissions(value: Any) -> frozenset[str] | None:
    """Reconstruct a fresh worker principal without trusting persisted shapes."""

    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("task Agent Runtime Principal permissions must be an array")
    return AgentRuntimePrincipal.create(permissions=value).permissions


@contextmanager
def _task_agent_principal_scope(
    permissions: frozenset[str] | None,
) -> Any:
    """Bind inherited grants for one worker attempt and revoke copied contexts."""

    if permissions is None:
        yield
        return
    binding = create_agent_principal_binding(
        AgentRuntimePrincipal.create(permissions=permissions)
    )
    try:
        with activate_agent_principal(binding):
            yield
    finally:
        revoke_agent_principal(binding)


def _validated_snapshot(value: Any) -> Mapping[str, Any] | None:
    """Validate persisted identity before reactivating runtime capabilities."""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("task invocation snapshot must be an object")
    expected = {
        "framework",
        "agent_name",
        "invocation_id",
        "user_id",
        "session_id",
        "metadata",
    }
    if set(value) != expected:
        raise ValueError("task invocation snapshot fields are invalid")
    if value["framework"] not in {"adk", "langgraph"}:
        raise ValueError("task invocation framework is invalid")
    for field in expected - {"framework", "metadata"}:
        if not isinstance(value[field], str) or not value[field]:
            raise ValueError(f"task invocation {field} is invalid")
    snapshot = dict(value)
    snapshot["metadata"] = safe_task_arguments(value["metadata"])
    return MappingProxyType(snapshot)


def _validated_trigger(value: Any) -> str:
    """Accept only bounded task origins persisted by this runtime."""

    if value not in {"agent", "user", "cron"}:
        raise ValueError("task payload trigger is invalid")
    return value


async def _resolve_task_call(
    function: Any, arguments: Mapping[str, Any]
) -> Any:
    """Execute sync task bodies off-loop and preserve their JSON-safe result."""

    if inspect.iscoroutinefunction(function):
        return await function(**dict(arguments))
    result = await asyncio.to_thread(function, **dict(arguments))
    if inspect.isawaitable(result):
        return await result
    return result


def _native_idempotency_key(
    application: Any,
    compiled: CompiledTask,
    arguments: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None,
) -> str | None:
    """Derive replay-stable queue ownership inside a native durable tool call."""

    native = current_native_durable_call()
    if native is None or snapshot is None:
        return None
    # Including safe arguments distinguishes separate submissions from one tool
    # without persisting their values in Procrastinate's queueing lock or logs.
    encoded = json.dumps(
        dict(arguments), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    argument_key = hashlib.sha256(encoded).hexdigest()
    return native.submission_key(
        application_id=application.name,
        user_id=snapshot["user_id"],
        session_id=snapshot["session_id"],
        run_id=snapshot["invocation_id"],
        provider=_CONTINUATION_PROVIDER,
        capability=f"task.defer:{compiled.name}:{argument_key}",
    )


def _validated_outcome(row: Mapping[str, Any]) -> tuple[str, Any]:
    """Validate private task state before a result crosses into authored code."""

    status = row.get("status", "pending")
    if status == "pending":
        return "pending", None
    if status == "completed":
        result = row.get("result")
        if not isinstance(result, Mapping) or set(result) != {"value"}:
            raise TaskRuntimeError("persisted task result envelope is invalid")
        return "completed", safe_task_result(result["value"])
    failure = row.get("failure_code")
    if status != "failed" or failure not in {_FAILED, _CANCELLED}:
        raise TaskRuntimeError("persisted task result state is invalid")
    return "failed", failure


def _is_no_result(backend: Any, error: BaseException) -> bool:
    """Recognize an absent connector row without importing the lazy backend."""

    no_result = getattr(getattr(backend, "exceptions", None), "NoResult", None)
    return isinstance(error, LookupError) or (
        isinstance(no_result, type) and isinstance(error, no_result)
    )


def _task_result(outcome: tuple[str, Any]) -> Any:
    """Restore the public result or its payload-free terminal failure."""

    if outcome[0] == "completed":
        return outcome[1]
    raise TaskExecutionError(f"task result failed: {outcome[1]}")


def _validate_continuation_result(value: Any) -> Any:
    """Validate the deterministic private envelope used by task callbacks."""

    if not isinstance(value, Mapping) or set(value) != {"value"}:
        raise TypeError("task continuation result envelope is invalid")
    return safe_task_result(value["value"])


def _is_final_attempt(job_context: Any, max_retries: int) -> bool:
    """Match Procrastinate's retry decision before publishing terminal failure."""

    job = getattr(job_context, "job", None)
    attempts = getattr(job, "attempts", None)
    if type(attempts) is not int or attempts < 0:
        raise TaskRuntimeError("task worker attempt metadata is invalid")
    return attempts >= max_retries


def _queueing_lock(task_name: str, idempotency_key: str | None) -> str | None:
    """Hash caller keys so neither native rows nor logs contain customer values."""

    if idempotency_key is None:
        return None
    digest = hashlib.sha256(
        f"{task_name}\0{idempotency_key}".encode("utf-8")
    ).hexdigest()
    return f"harnest:{digest}"


def _payload_id(task_name: str, queueing_lock: str | None) -> str:
    """Use stable private identity only when the submission is idempotent."""

    if queueing_lock is None:
        return uuid.uuid4().hex
    return hashlib.sha256(
        f"{task_name}\0{queueing_lock}".encode("utf-8")
    ).hexdigest()


def _load_procrastinate() -> Any:
    """Import the compiler-selected backend only for applications with tasks."""

    try:
        return importlib.import_module("procrastinate")
    except ImportError:
        raise TaskRuntimeError(
            "compiled tasks require the Procrastinate runtime dependency"
        ) from None


async def _await_cancelled(task: asyncio.Task[Any]) -> None:
    """Consume worker cancellation without suppressing an earlier startup failure."""

    try:
        await task
    except asyncio.CancelledError:
        return
    except Exception:
        return


async def _cleanup_failure(callback: Any) -> BaseException | None:
    """Capture cleanup failures so all owners still receive their close call."""

    try:
        await callback()
    except BaseException as error:
        return error
    return None


def _audit(operation: str, trigger: str, outcome: str) -> None:
    """Write one payload-free user/agent mutation signal through OTEL logging."""

    event = f"task.{operation}"
    _AUDIT.info(
        event,
        operation=event,
        trigger=trigger,
        outcome=outcome,
        backend="procrastinate",
    )


def _cron_audit(operation: str, schedule: str, outcome: str) -> None:
    """Write a payload-free cron mutation signal with stable schedule identity."""

    event = f"task.cron.{operation}"
    _AUDIT.info(
        event,
        operation=event,
        trigger="cron",
        outcome=outcome,
        backend="procrastinate",
        schedule=schedule,
    )


__all__ = ["TaskRuntimeDriver", "TaskRuntimeError", "TaskRuntimeManager"]
