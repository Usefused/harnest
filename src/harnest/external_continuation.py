"""Exact-coroutine waits backed by provider-neutral durable continuations."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
import time
from typing import Any, Iterator

from .approval import ApprovalRun
from .continuation import (
    ContinuationConflictError,
    ContinuationFailure,
    ContinuationProvider,
    ContinuationRecord,
    ContinuationStore,
    ContinuationValidator,
    ProviderPendingContinuation,
)
from .checkpoint import RunRecord, RunScope
from .durable import (
    NativeDurableSuspended,
    NativeResumeInput,
    current_native_durable_call,
)
from .runtime_contract import InvocationRequest


_FAILURE_KEY = "__harnest_external_failure_v1__"


class ExternalContinuationUnavailableError(RuntimeError):
    """A plugin tried to wait outside a managed Harnest invocation."""


class ExternalContinuationFailed(RuntimeError):
    """An external runtime completed work with a payload-free failure code."""

    def __init__(self, code: str) -> None:
        """Retain only the validated failure category safe for agent code."""

        super().__init__(f"external continuation failed: {code}")
        self.code = code


@dataclass(frozen=True, slots=True)
class PendingExternalContinuation:
    """Opaque public wait metadata that never exposes provider job identity."""

    id: str
    capability: str

    def public(self) -> dict[str, str]:
        """Render the stable response fragment shared by every transport."""

        return {
            "type": "external_continuation",
            "id": self.id,
            "capability": self.capability,
        }


@dataclass(slots=True)
class _Execution:
    """Invocation identity needed to persist and announce one plugin wait."""

    runtime: "ExternalContinuationRuntime"
    run: ApprovalRun
    request: InvocationRequest


@dataclass(slots=True)
class _ResponseState:
    """Bounded poll state for an invocation that crossed an external boundary."""

    request: InvocationRequest
    run: ApprovalRun
    provider: str
    pending: PendingExternalContinuation
    final: dict[str, Any] | None = None
    updated_at: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


_CURRENT: ContextVar[_Execution | None] = ContextVar(
    "harnest_external_continuation_execution", default=None
)


class ContinuationHandle:
    """One plugin-owned handle that enters framework-native suspension."""

    __slots__ = ("_call", "_pending")

    def __init__(self, call: Any, pending: PendingExternalContinuation) -> None:
        self._call = call
        self._pending = pending

    async def result(self) -> Any:
        """Pause through ADK or LangGraph instead of retaining this coroutine."""

        value = self._call.suspend(self._pending)
        if isinstance(value, dict) and set(value) == {_FAILURE_KEY}:
            raise ExternalContinuationFailed(value[_FAILURE_KEY])
        return value


class InvocationContinuationPort:
    """Provider-bound suspension authority exposed through PluginContext."""

    __slots__ = ("_provider", "_runtime")

    def __init__(
        self, runtime: "ExternalContinuationRuntime", provider: str
    ) -> None:
        self._runtime = runtime
        self._provider = provider

    async def suspend(
        self,
        external_id: str,
        *,
        capability: str,
        schema_id: str,
        validate: ContinuationValidator,
    ) -> ContinuationHandle:
        """Persist one wait before returning a handle to plugin-owned code."""

        execution = _CURRENT.get()
        if execution is None or execution.runtime is not self._runtime:
            raise ExternalContinuationUnavailableError(
                "external continuations require a managed invocation"
            )
        return await self._runtime.suspend(
            execution,
            provider=self._provider,
            external_id=external_id,
            capability=capability,
            schema_id=schema_id,
            validate=validate,
        )


class ApplicationContinuationPort:
    """Provider-bound completion authority retained by a started plugin."""

    __slots__ = ("_provider", "_runtime")

    def __init__(
        self, runtime: "ExternalContinuationRuntime", provider: str
    ) -> None:
        self._runtime = runtime
        self._provider = provider

    async def complete(self, external_id: str, result: Any) -> ContinuationRecord:
        """Commit validated output and resume through any claiming replica."""

        return await self._runtime.complete(
            provider=self._provider, external_id=external_id, result=result
        )

    async def fail(self, external_id: str, error_code: str) -> ContinuationRecord:
        """Commit a payload-free failure and resume through the framework."""

        return await self._runtime.fail(
            provider=self._provider,
            external_id=external_id,
            error_code=error_code,
        )

    async def cancel(
        self, record: ContinuationRecord, error_code: str
    ) -> ContinuationRecord:
        """Cancel one provider wait and its run without resuming agent code."""

        return await self._runtime.cancel_wait(
            provider=self._provider,
            record=record,
            error_code=error_code,
        )

    async def list_pending(
        self, *, after: str | None = None, limit: int = 100
    ) -> Sequence[ProviderPendingContinuation]:
        """Return a bounded durable page for provider reconciliation."""

        return await self._runtime.provider(self._provider).list_pending(
            after=after, limit=limit
        )

    def register_schema(
        self, schema_id: str, validate: ContinuationValidator
    ) -> None:
        """Register restart-safe result validation during plugin startup."""

        self._runtime.register_schema(self._provider, schema_id, validate)


class ExternalContinuationRuntime:
    """Coordinate durable provider state with framework-native resumption."""

    def __init__(
        self,
        store: ContinuationStore,
        *,
        application_id: str,
        max_responses: int = 1024,
        tombstone_seconds: float = 60,
    ) -> None:
        """Bind one application while keeping provider authority non-enumerable."""

        if max_responses < 1:
            raise ValueError("continuation max_responses must be at least one")
        if tombstone_seconds < 0:
            raise ValueError("continuation tombstone_seconds cannot be negative")
        self._store = store
        self._application_id = application_id
        self._max_responses = max_responses
        self._tombstone_seconds = tombstone_seconds
        self._providers: dict[str, ContinuationProvider] = {}
        self._validators: dict[tuple[str, str], ContinuationValidator] = {}
        self._cancel_handlers: dict[
            str, Callable[[ProviderPendingContinuation], Awaitable[bool]]
        ] = {}
        self._responses: OrderedDict[str, _ResponseState] = OrderedDict()
        self._response_lock = Lock()
        self._driver: Any | None = None
        self._closed = False

    def bind_driver(self, driver: Any) -> None:
        """Attach the fully wrapped runtime once so callbacks can re-enter it."""

        if self._driver is not None and self._driver is not driver:
            raise RuntimeError("continuation runtime driver is already bound")
        if not callable(getattr(driver, "invoke", None)):
            raise TypeError("continuation runtime driver must implement invoke")
        self._driver = driver

    def register_schema(
        self, provider: str, schema_id: str, validate: ContinuationValidator
    ) -> None:
        """Bind stable validation code before replica reconciliation begins."""

        if not callable(validate):
            raise TypeError("continuation validator must be callable")
        key = (provider, schema_id)
        existing = self._validators.get(key)
        if existing is not None and existing is not validate:
            raise RuntimeError("continuation schema validator is already registered")
        self._validators[key] = validate

    def register_cancel_handler(
        self,
        provider: str,
        handler: Callable[[ProviderPendingContinuation], Awaitable[bool]],
    ) -> None:
        """Bind one provider-owned cancellation hook for transport task APIs."""

        if not callable(handler):
            raise TypeError("continuation cancel handler must be callable")
        existing = self._cancel_handlers.get(provider)
        if existing is not None and existing is not handler:
            raise RuntimeError("continuation cancel handler is already registered")
        self.provider(provider)
        self._cancel_handlers[provider] = handler

    def provider(self, name: str) -> ContinuationProvider:
        """Return one cached facade whose provider identity cannot be replaced."""

        provider = self._providers.get(name)
        if provider is None:
            provider = ContinuationProvider(
                self._store,
                application_id=self._application_id,
                provider=name,
            )
            self._providers[name] = provider
        return provider

    def application_port(self, provider: str) -> ApplicationContinuationPort:
        """Build startup authority already constrained to one plugin name."""

        self.provider(provider)
        return ApplicationContinuationPort(self, provider)

    def invocation_port(self, provider: str) -> InvocationContinuationPort:
        """Build invocation authority already constrained to one plugin name."""

        self.provider(provider)
        return InvocationContinuationPort(self, provider)

    @contextmanager
    def execution(
        self, run: ApprovalRun, request: InvocationRequest
    ) -> Iterator[None]:
        """Bind one invocation so plugin contexts cannot forge tenant scope."""

        token = _CURRENT.set(_Execution(self, run, request))
        try:
            yield
        finally:
            _CURRENT.reset(token)

    async def suspend(
        self,
        execution: _Execution,
        *,
        provider: str,
        external_id: str,
        capability: str,
        schema_id: str,
        validate: ContinuationValidator,
    ) -> ContinuationHandle:
        """Persist and publish a wait before entering native suspension."""

        self._require_open()
        native = current_native_durable_call()
        if native is None:
            raise ExternalContinuationUnavailableError(
                "unfinished external work requires @tool(durable=True)"
            )
        if execution.request.agent_principal is not None:
            # Stored continuations can resume on another replica, where the
            # opaque invocation authority is deliberately unavailable. Failing
            # before persistence prevents a restricted run resuming unrestricted.
            raise ExternalContinuationUnavailableError(
                "external continuations do not support Agent Runtime Principal "
                "resumption"
            )
        self.register_schema(provider, schema_id, validate)
        request = execution.request
        existing = await self.provider(provider).lookup(external_id)
        if existing is not None:
            return self._restore_wait(
                execution,
                provider=provider,
                capability=capability,
                schema_id=schema_id,
                native=native,
                record=existing.record,
            )
        # Reserve bounded poll capacity before changing durable run state. A
        # successful provider write must never leave a run that HTTP cannot expose.
        reserved = self._reserve_response(
            execution.run, request, provider, capability
        )
        try:
            record = await self.provider(provider).suspend(
                user_id=request.user_id,
                session_id=request.session_id,
                run_id=request.invocation_id,
                external_id=external_id,
                capability=capability,
                schema_id=schema_id,
                resume=native.artifact,
            )
        except BaseException:
            if reserved:
                self._discard_response(request.invocation_id, execution.run)
            raise
        pending = PendingExternalContinuation(record.continuation_id, capability)
        self._set_pending_response(request.invocation_id, pending)
        execution.run.notifications.put_nowait(("external_continuation", pending))
        return ContinuationHandle(native, pending)

    def _restore_wait(
        self,
        execution: _Execution,
        *,
        provider: str,
        capability: str,
        schema_id: str,
        native: Any,
        record: ContinuationRecord,
    ) -> ContinuationHandle:
        """Recreate one native handle when LangGraph replays its durable node."""

        request = execution.request
        valid = (
            record.application_id == self._application_id
            and record.user_id == request.user_id
            and record.session_id == request.session_id
            and record.run_id == request.invocation_id
            and record.provider == provider
            and record.capability == capability
            and record.schema_id == schema_id
            and record.resume == native.artifact
        )
        if not valid:
            raise ContinuationConflictError("external continuation ownership changed")
        self._reserve_response(execution.run, request, provider, capability)
        pending = PendingExternalContinuation(record.continuation_id, capability)
        self._set_pending_response(request.invocation_id, pending)
        # Native resume already owns the value that consumes this interrupt.
        # Announcing another in-progress boundary would regress the HTTP state.
        if not isinstance(request.input, NativeResumeInput):
            execution.run.notifications.put_nowait(
                ("external_continuation", pending)
            )
        return ContinuationHandle(native, pending)

    async def complete(
        self, *, provider: str, external_id: str, result: Any
    ) -> ContinuationRecord:
        """Resolve success by durable provider identity, independent of process."""

        record = await self._record_for_external(provider, external_id)
        validate = self._validator(provider, record.schema_id)
        resolved = await self.provider(provider).complete(
            user_id=record.user_id,
            session_id=record.session_id,
            run_id=record.run_id,
            external_id=external_id,
            schema_id=record.schema_id,
            result=result,
            validate=validate,
        )
        return await self._resume_if_ready(provider, resolved)

    async def fail(
        self, *, provider: str, external_id: str, error_code: str
    ) -> ContinuationRecord:
        """Resolve failure by durable provider identity, independent of process."""

        record = await self._record_for_external(provider, external_id)
        failure = ContinuationFailure(error_code)
        resolved = await self.provider(provider).fail(
            user_id=record.user_id,
            session_id=record.session_id,
            run_id=record.run_id,
            external_id=external_id,
            schema_id=record.schema_id,
            failure=failure,
        )
        return await self._resume_if_ready(provider, resolved)

    async def cancel_wait(
        self,
        *,
        provider: str,
        record: ContinuationRecord,
        error_code: str,
    ) -> ContinuationRecord:
        """Commit provider cancellation without replaying the durable tool."""

        self._require_open()
        failure = ContinuationFailure(error_code)
        return await self.provider(provider).cancel(record, failure)

    async def cancel_task_wait(
        self, *, response_id: str, user_id: str, session_id: str
    ) -> bool:
        """Cancel an exact durable wait only through its registered provider."""

        self._require_open()
        scope = RunScope(self._application_id, user_id, session_id, response_id)
        get_run = getattr(self._store, "get_run", None)
        if not callable(get_run):
            return False
        run = await get_run(scope=scope)
        pending_action = None if run is None else run.pending_action
        if (
            run is None
            or run.status != "waiting"
            or pending_action is None
            or pending_action.type != "external_continuation"
        ):
            return False
        record = await self._store.get_continuation(
            scope=scope,
            continuation_id=pending_action.action_id,
        )
        if record is None:
            return False
        handler = self._cancel_handlers.get(record.provider)
        if handler is None:
            return False
        pending = await self.provider(record.provider).get_pending(
            user_id=user_id,
            session_id=session_id,
            run_id=response_id,
            continuation_id=record.continuation_id,
        )
        if pending is None:
            return False
        return await handler(pending)

    async def arm(
        self, *, response_id: str, user_id: str, session_id: str
    ) -> ContinuationRecord:
        """Arm the current wait after its native framework checkpoint commits."""

        state = self._response(response_id, user_id=user_id, session_id=session_id)
        if state is None:
            raise KeyError("continuation response not found")
        record = await self.provider(state.provider).get(
            user_id=user_id,
            session_id=session_id,
            run_id=response_id,
            continuation_id=state.pending.id,
        )
        if record is None:
            raise KeyError("continuation record not found")
        armed = await self._arm_record(state.provider, record)
        return await self._resume_if_ready(state.provider, armed)

    async def response_boundary(
        self, *, response_id: str, user_id: str, session_id: str
    ) -> tuple[str, Any]:
        """Read local events or converge through the shared durable run state."""

        state = self._response(response_id, user_id=user_id, session_id=session_id)
        if state is None:
            durable = await self._durable_boundary(
                response_id, user_id, session_id
            )
            if durable is None:
                raise KeyError("continuation response not found")
            if durable[0] == "external_continuation":
                self._remember_durable_pending(
                    response_id, user_id, session_id, durable[1]
                )
            return durable
        async with state.lock:
            if state.final is not None:
                return "final", dict(state.final)
            local = self._local_boundary(state)
            if local[0] != "external_continuation":
                return local
            # The callback replica cannot notify the origin process. Checking
            # the scoped run after local events lets either replica observe the
            # framework commit without retaining a distributed Python Future.
            durable = await self._durable_boundary(
                response_id, user_id, session_id
            )
            if durable is not None:
                if durable[0] == "external_continuation":
                    state.pending = durable[1]
                    state.updated_at = time.monotonic()
                return durable
            return local

    @staticmethod
    def _local_boundary(state: _ResponseState) -> tuple[str, Any]:
        """Prefer an exact local terminal event while retaining the latest wait."""

        boundary: tuple[str, Any] = ("external_continuation", state.pending)
        while True:
            try:
                kind, value = state.run.notifications.get_nowait()
            except asyncio.QueueEmpty:
                return boundary
            if kind == "event":
                continue
            if kind == "external_continuation":
                state.pending = value
                state.updated_at = time.monotonic()
                boundary = (kind, value)
                continue
            return kind, value

    async def _durable_boundary(
        self, response_id: str, user_id: str, session_id: str
    ) -> tuple[str, Any] | None:
        """Resolve an external wait or terminal through complete run ownership."""

        get_run = getattr(self._store, "get_run", None)
        if not callable(get_run):
            return None
        run = await get_run(
            scope=RunScope(
                self._application_id,
                user_id,
                session_id,
                response_id,
            )
        )
        if run is None:
            return None
        pending = run.pending_action
        if (
            run.status == "waiting"
            and pending is not None
            and pending.type == "external_continuation"
        ):
            return (
                "external_continuation",
                PendingExternalContinuation(pending.action_id, pending.capability),
            )
        if run.status in {"completed", "failed", "cancelled"}:
            return "durable_terminal", run
        return None

    def _remember_durable_pending(
        self,
        response_id: str,
        user_id: str,
        session_id: str,
        pending: PendingExternalContinuation,
    ) -> None:
        """Retain a shared wait locally across the short claimed/running window."""

        with self._response_lock:
            self._prune_responses_locked()
            if response_id in self._responses:
                return
            self._evict_oldest_terminal_locked()
            if len(self._responses) >= self._max_responses:
                return
            self._responses[response_id] = self._poll_response_state(
                response_id, user_id, session_id, pending
            )

    def retain_final(
        self,
        *,
        response_id: str,
        user_id: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Cache one already-sanitized wire result for idempotent status reads."""

        with self._response_lock:
            self._prune_responses_locked()
            state = self._responses.get(response_id)
            if state is not None and (
                state.request.user_id != user_id
                or state.request.session_id != session_id
            ):
                raise KeyError("continuation response not found")
            if state is None:
                state = self._terminal_response_state(
                    response_id, user_id, session_id
                )
            if state is None:
                # Active waits own capacity ahead of terminal tombstones. A
                # stateless replica can still reconstruct on its next poll.
                return
            state.final = dict(payload)
            state.updated_at = time.monotonic()
            self._responses.move_to_end(response_id)
            self._prune_responses_locked()

    def _terminal_response_state(
        self, response_id: str, user_id: str, session_id: str
    ) -> _ResponseState | None:
        """Reserve a bounded tombstone for a terminal reconstructed response."""

        self._evict_oldest_terminal_locked()
        if len(self._responses) >= self._max_responses:
            return None
        state = self._poll_response_state(
            response_id,
            user_id,
            session_id,
            PendingExternalContinuation("terminal", "terminal"),
        )
        self._responses[response_id] = state
        return state

    @staticmethod
    def _poll_response_state(
        response_id: str,
        user_id: str,
        session_id: str,
        pending: PendingExternalContinuation,
    ) -> _ResponseState:
        """Build process-local state without inventing provider authority."""

        request = InvocationRequest(
            input="",
            user_id=user_id,
            session_id=session_id,
            invocation_id=response_id,
            metadata={},
            state_delta={},
            transport="continuation_poll",
        )
        run = ApprovalRun(
            id=response_id,
            user_id=user_id,
            session_id=session_id,
            call_id=response_id,
        )
        return _ResponseState(request, run, "", pending)

    def _evict_oldest_terminal_locked(self) -> None:
        """Prefer replacing a tombstone over rejecting a newly observed result."""

        if len(self._responses) < self._max_responses:
            return
        for response_id, state in self._responses.items():
            if state.final is not None:
                self._responses.pop(response_id)
                return

    async def close(self) -> None:
        """Release process-local response state without cancelling provider work."""

        if self._closed:
            return
        self._closed = True
        self._cancel_handlers.clear()
        with self._response_lock:
            self._responses.clear()

    async def _record_for_external(
        self, provider: str, external_id: str
    ) -> ContinuationRecord:
        """Resolve one callback through the provider's durable external index."""

        self._require_open()
        pending = await self.provider(provider).lookup(external_id)
        if pending is None:
            raise KeyError("external continuation was not found")
        return pending.record

    def _validator(
        self, provider: str, schema_id: str
    ) -> ContinuationValidator:
        """Fail closed when a replica did not register the persisted schema."""

        validate = self._validators.get((provider, schema_id))
        if validate is None:
            raise ExternalContinuationUnavailableError(
                "external continuation schema is not registered"
            )
        return validate

    async def _claim(
        self, provider: str, record: ContinuationRecord
    ) -> ContinuationRecord:
        """Move the portable run back to running before tool code continues."""

        return await self.provider(provider).claim(
            user_id=record.user_id,
            session_id=record.session_id,
            run_id=record.run_id,
            continuation_id=record.continuation_id,
            expected_revision=record.revision,
        )

    async def _arm_record(
        self, provider: str, record: ContinuationRecord
    ) -> ContinuationRecord:
        """Retry one CAS when a simultaneous callback advanced the outcome."""

        try:
            return await self.provider(provider).arm(record)
        except ContinuationConflictError:
            latest = await self.provider(provider).get(
                user_id=record.user_id,
                session_id=record.session_id,
                run_id=record.run_id,
                continuation_id=record.continuation_id,
            )
            if latest is None:
                raise
            if latest.ready:
                return latest
            return await self.provider(provider).arm(latest)

    async def _resume_if_ready(
        self, provider: str, record: ContinuationRecord
    ) -> ContinuationRecord:
        """Claim and dispatch only after both outcome and checkpoint are durable."""

        if not record.ready or record.status not in {"completed", "failed"}:
            return record
        try:
            claimed = await self._claim(provider, record)
        except ContinuationConflictError:
            latest = await self.provider(provider).get(
                user_id=record.user_id,
                session_id=record.session_id,
                run_id=record.run_id,
                continuation_id=record.continuation_id,
            )
            # A racing callback and checkpoint arm may both observe readiness;
            # the durable claimed state proves the other replica owns dispatch.
            if latest is not None and latest.status == "claimed":
                return latest
            raise
        await self._resume(claimed)
        return claimed

    async def _resume(self, record: ContinuationRecord) -> None:
        """Re-enter the wrapped runtime with the persisted framework identity."""

        if self._driver is None:
            raise ExternalContinuationUnavailableError(
                "external continuation resume driver is unavailable"
            )
        if record.resume is None:
            raise ExternalContinuationUnavailableError(
                "external continuation has no framework resume identity"
            )
        value = _resume_value(record)
        request = InvocationRequest(
            input=NativeResumeInput(record.resume, value),
            user_id=record.user_id,
            session_id=record.session_id,
            invocation_id=record.run_id,
            metadata={},
            state_delta={},
            transport="continuation",
        )
        run = ApprovalRun(
            id=record.run_id,
            user_id=record.user_id,
            session_id=record.session_id,
            call_id=record.run_id,
        )
        try:
            with self.execution(run, request):
                result = await self._driver.invoke(request)
        except NativeDurableSuspended:
            await self.arm(
                response_id=record.run_id,
                user_id=record.user_id,
                session_id=record.session_id,
            )
            return
        self._notify_result(record.run_id, result)

    def _notify_result(self, response_id: str, result: Any) -> None:
        """Wake same-process polling while durable session state serves replicas."""

        with self._response_lock:
            state = self._responses.get(response_id)
        if state is not None:
            state.run.notifications.put_nowait(("result", result))

    def _reserve_response(
        self,
        run: ApprovalRun,
        request: InvocationRequest,
        provider: str,
        capability: str,
    ) -> bool:
        """Reserve bounded poll capacity before a durable suspension commits."""

        with self._response_lock:
            self._prune_responses_locked()
            state = self._responses.get(request.invocation_id)
            if state is not None:
                return False
            if len(self._responses) >= self._max_responses:
                raise ExternalContinuationUnavailableError(
                    "external continuation response capacity is exhausted"
                )
            # This value cannot escape: the response id is returned only after
            # the durable provider record replaces this reservation.
            pending = PendingExternalContinuation("reserved", capability)
            self._responses[request.invocation_id] = _ResponseState(
                request, run, provider, pending
            )
            return True

    def _discard_response(self, response_id: str, run: ApprovalRun) -> None:
        """Release only the failed suspension's own capacity reservation."""

        with self._response_lock:
            state = self._responses.get(response_id)
            if state is not None and state.run is run:
                self._responses.pop(response_id, None)

    def _set_pending_response(
        self, response_id: str, pending: PendingExternalContinuation
    ) -> None:
        """Publish durable opaque identity into a previously reserved response."""

        with self._response_lock:
            state = self._responses.get(response_id)
            if state is None:
                raise ExternalContinuationUnavailableError(
                    "external continuation response reservation was lost"
                )
            state.pending = pending
            state.updated_at = time.monotonic()
            self._responses.move_to_end(response_id)

    def _response(
        self, response_id: str, *, user_id: str, session_id: str
    ) -> _ResponseState | None:
        """Authorize every ownership field without revealing mismatched records."""

        with self._response_lock:
            self._prune_responses_locked()
            state = self._responses.get(response_id)
            if state is None:
                return None
            request = state.request
            if request.user_id != user_id or request.session_id != session_id:
                return None
            self._responses.move_to_end(response_id)
            return state

    def _prune_responses_locked(self) -> None:
        """Remove expired terminal tombstones while retaining every active task."""

        threshold = time.monotonic() - self._tombstone_seconds
        expired = tuple(
            response_id
            for response_id, state in self._responses.items()
            if state.final is not None and state.updated_at <= threshold
        )
        for response_id in expired:
            self._responses.pop(response_id, None)

    def _require_open(self) -> None:
        """Fail closed after the application has begun shutting down."""

        if self._closed:
            raise ExternalContinuationUnavailableError(
                "external continuation runtime is closed"
            )


def _resume_value(record: ContinuationRecord) -> Any:
    """Translate a stored outcome into one framework-safe resume value."""

    if record.failure is None:
        return record.result
    if record.resume is not None and record.resume.framework == "langgraph":
        # LangGraph re-enters the tool, so its handle can restore the authored
        # exception contract without placing an exception in the checkpoint.
        return {_FAILURE_KEY: record.failure.code}
    # ADK resumes the model loop rather than the Python tool frame. Give the
    # model a payload-free error category as the long-running FunctionResponse.
    return {
        "error": {
            "code": record.failure.code,
            "retryable": record.failure.retryable,
        }
    }


__all__ = [
    "ApplicationContinuationPort",
    "ContinuationHandle",
    "ExternalContinuationFailed",
    "ExternalContinuationRuntime",
    "ExternalContinuationUnavailableError",
    "InvocationContinuationPort",
    "PendingExternalContinuation",
]
