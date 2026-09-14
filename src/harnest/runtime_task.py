"""Shared invocation and continuation ownership for storage-backed task workers."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager, nullcontext
import hashlib
import inspect
import json
from types import MappingProxyType
from typing import Any, AsyncIterator, Mapping, Sequence

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
    create_agent_context,
    revoke_context,
)
from . import context
from .context_session import invocation_session_context
from .context_agent import LocalAgentRuntime, activate_context_agent
from .continuation import (
    ContinuationConflictError,
    ProviderPendingContinuation,
)
from .cron import CompiledCron, _activate_runtime as activate_cron_runtime
from .credentials import _activate_credential_provider
from .durable import current_native_durable_call
from .external_continuation import (
    ExternalContinuationFailed,
    ExternalContinuationRuntime,
)
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
    safe_task_arguments,
    safe_task_result,
)


_CONTINUATION_PROVIDER = "harnest.task"
_RESULT_CAPABILITY = "task.result"
_RESULT_SCHEMA = "harnest.task.result.v1"
_CANCELLED = "task_cancelled"
_AUTOMATION_USER_ID = "_harnest_automation"


class TaskRuntimeError(RuntimeError):
    """Task storage or execution failed at a payload-safe runtime boundary."""


class TaskExecutionError(RuntimeError):
    """An authored task failed without exposing its exception message."""


class TaskExecutionRuntime:
    """Share task identity, capability scopes, and continuation delivery."""

    def __init__(
        self,
        application: Any,
        *,
        extension_manager: Any | None = None,
        continuation_runtime: ExternalContinuationRuntime | None = None,
        automation_user_id: str = _AUTOMATION_USER_ID,
        enable_cron: bool = True,
    ) -> None:
        """Retain execution ownership without selecting a database or queue library."""

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
        self._extension_manager = extension_manager
        self._continuation_runtime: ExternalContinuationRuntime | None = None
        self._invocation_continuations: Any | None = None
        self._application_continuations: Any | None = None
        self._agent_driver: RuntimeDriver | None = None
        self._automation_user_id = _automation_identity(automation_user_id)
        self._task_by_name = {item.name: item for item in tasks}
        self._dynamic_cron: Any | None = None
        self._worker: asyncio.Task[Any] | None = None
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

    @property
    def cron_runtime(self) -> Any:
        """Expose the scoped cron capability to the owning runtime driver."""

        return self._dynamic_cron

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
            self._audit_runtime("cancel", "agent", "failed")
            raise TaskRuntimeError(
                "durable task cancellation failed with "
                f"{type(error).__name__}"
            ) from None
        self._audit_runtime("cancel", "agent", "committed")
        return True


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
                self._audit_runtime("result.reconcile", "agent", "failed")
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
            self._audit_runtime("result.reconcile", "agent", "failed")


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
        with (
            activate_cron_runtime(self._dynamic_cron),
            _task_agent_principal_scope(agent_permissions),
        ):
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
            if self._extension_manager is None
            else self._extension_manager.invocation_bindings()
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
            memory_store=capabilities.memory_store,
            memory_application_id=self._application.name,
            skill_registry=capabilities.skill_registry,
            sandbox_registry=capabilities.sandbox_registry,
            extension_bindings=bindings,
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
            self._audit_runtime("result.notify", "agent", "failed")


    def _compiled_for(self, task_value: TaskCallable[Any]) -> CompiledTask:
        """Resolve only a callable bound by this compiler-owned runtime."""

        for compiled in self._tasks:
            if compiled.authored is task_value:
                return compiled
        raise TaskRuntimeError("task callable does not belong to this runtime")


    def _require_handle(self, handle: TaskHandle) -> None:
        """Reject forged or cross-runtime handles before native database access."""

        if not isinstance(handle, TaskHandle) or handle._runtime is not self:
            raise TaskRuntimeError("task handle does not belong to this runtime")
        if handle.task_name not in self._task_by_name:
            raise TaskRuntimeError("task handle references an unknown task")


    def _worker_done(self, worker: asyncio.Task[Any]) -> None:
        """Consume terminal worker state so background failures are never lost."""

        if worker.cancelled() or self._state in {"closing", "closed"}:
            return
        error = worker.exception()
        suffix = "unexpectedly" if error is None else f"with {type(error).__name__}"
        self._worker_failure = TaskRuntimeError(f"task worker stopped {suffix}")
        self._audit_runtime("worker", "agent", "failed")


class TaskRuntimeDriver(RuntimeDriver):
    """Start queue workers after inner capabilities and stop them before teardown."""

    def __init__(self, driver: RuntimeDriver, manager: TaskExecutionRuntime) -> None:
        """Retain explicit ownership without importing the optional backend."""

        if not isinstance(manager, TaskExecutionRuntime):
            raise TypeError("manager must be a storage-backed task runtime")
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

    async def create_session_with_plugins(
        self,
        *,
        session_id: str,
        user_id: str,
        state: Mapping[str, Any],
        plugins: Sequence[Mapping[str, Any]],
    ) -> SessionRecord:
        """Start task workers before accepting fixed session capabilities."""

        await self.start()
        from .dynamic_agent_plugins import forward_session_plugins

        return await forward_session_plugins(
            self._driver,
            session_id=session_id,
            user_id=user_id,
            state=state,
            plugins=plugins,
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
        with activate_cron_runtime(self._manager.cron_runtime):
            return await self._driver.invoke(request)

    async def stream(
        self, request: InvocationRequest
    ) -> AsyncIterator[RuntimeEvent]:
        await self.start()
        iterator = self._driver.stream(request).__aiter__()
        try:
            while True:
                try:
                    # The capability is active only while authored runtime code
                    # advances; callers cannot use a yielded stream as authority.
                    with activate_cron_runtime(self._manager.cron_runtime):
                        event = await anext(iterator)
                except StopAsyncIteration:
                    return
                yield event
        finally:
            close = getattr(iterator, "aclose", None)
            if callable(close):
                with activate_cron_runtime(self._manager.cron_runtime):
                    await close()

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
    # without persisting their values in the idempotency key or logs.
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


async def _cleanup_failure(callback: Any) -> BaseException | None:
    """Capture cleanup failures so all owners still receive their close call."""

    try:
        await callback()
    except BaseException as error:
        return error
    return None


__all__ = ["TaskRuntimeDriver", "TaskRuntimeError", "TaskExecutionRuntime"]
