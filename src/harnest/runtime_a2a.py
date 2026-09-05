"""A2A 1.x server adapter for the framework-neutral Harnest runtime."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Mapping
from urllib.parse import urlsplit

from google.protobuf import json_format
from google.protobuf.timestamp_pb2 import Timestamp

from a2a import helpers
from a2a.auth.user import User
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_jsonrpc_routes, create_rest_routes
from a2a.server.routes.common import (
    DefaultServerCallContextBuilder,
    ServerCallContextBuilder,
)
from a2a.server.tasks import InMemoryTaskStore, TaskStore, TaskUpdater
from a2a.types import (
    AgentCard,
    Message,
    Part,
    Role,
    Task,
    TaskState,
    TaskStatus,
)
from a2a.utils.errors import (
    ContentTypeNotSupportedError,
    TaskNotCancelableError,
    TaskNotFoundError,
    UnsupportedOperationError,
)
from starlette.exceptions import HTTPException

from .approval import ApprovalDenied, ApprovalRun, PendingApproval
from .client_tool import PendingClientTool
from .output import _agent_metadata_from_runtime_event, _aggregate_token_usage
from .runtime_auth import (
    AuthPrincipal,
    _activate_task_principal,
    principal_for,
)
from .runtime_continuation import next_run_boundary, start_approval_run
from .runtime_contract import (
    InvocationRequest,
    InvocationResult,
    SessionConflictError,
    require_customer_facing_output,
)
from .runtime_invocation import InvocationCoordinator


_STREAMING_STATE_KEY = "harnest.a2a.streaming"
_PRINCIPAL_STATE_KEY = "harnest.a2a.principal"
_TERMINAL_STATES = frozenset(
    {
        TaskState.TASK_STATE_COMPLETED,
        TaskState.TASK_STATE_FAILED,
        TaskState.TASK_STATE_CANCELED,
        TaskState.TASK_STATE_REJECTED,
    }
)


class _HarnestA2AUser(User):
    """Expose only Harnest's stable principal ID to the A2A task store."""

    def __init__(self, principal: AuthPrincipal) -> None:
        self._principal = principal

    @property
    def is_authenticated(self) -> bool:
        """Treat the development principal as an owner even without auth middleware."""

        return True

    @property
    def user_name(self) -> str:
        """Return the privacy-safe identity used by Harnest session ownership."""

        return self._principal.user_id


class _HarnestCallContextBuilder(DefaultServerCallContextBuilder):
    """Bridge verified Harnest identity into the official A2A request context."""

    def build(self, request: Any) -> ServerCallContext:
        """Retain the opaque principal privately while exposing only its ID."""

        context = super().build(request)
        principal = principal_for(request)
        context.user = _HarnestA2AUser(principal)
        context.state[_PRINCIPAL_STATE_KEY] = principal
        return context


@dataclass(slots=True)
class _SuspendedExecution:
    """Retain only the authority needed to resume one interrupted A2A task."""

    kind: str
    run: ApprovalRun | None
    pending: Any
    request: InvocationRequest
    principal: AuthPrincipal


@dataclass(slots=True)
class _PublishedTask:
    """Track artifact publication without duplicating final text."""

    task: Task
    updater: TaskUpdater
    text_started: bool = False


class HarnestA2AExecutor(AgentExecutor):
    """Translate official A2A task lifecycles into neutral Harnest invocations."""

    def __init__(
        self,
        coordinator: InvocationCoordinator,
        task_store: TaskStore,
    ) -> None:
        """Share invocation limits, continuation stores, and task persistence."""

        self._coordinator = coordinator
        self._task_store = task_store
        self._active: dict[str, ApprovalRun] = {}
        self._suspended: dict[str, _SuspendedExecution] = {}
        self._lock = asyncio.Lock()

    async def execute(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        """Run or resume one task under an explicitly bounded principal lease."""

        principal = _request_principal(context.call_context)
        with _activate_task_principal(principal):
            suspended = await self._suspended_execution(context.task_id)
            if suspended is not None:
                await self._resume(context, event_queue, suspended)
                return
            await self._execute_new(context, event_queue, principal)

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        """Cancel only a live Harnest run and publish the A2A terminal state."""

        task_id = _required_identifier(context.task_id, "task")
        context_id = _required_identifier(context.context_id, "context")
        durable = await self.cancel_durable_task(task_id, context.call_context)
        if durable is not None:
            # A live SDK consumer still needs the event to terminate; a closed
            # queue drops it, after which the handler reasserts durable state.
            updater = TaskUpdater(event_queue, task_id, context_id)
            await updater.cancel(helpers.new_text_message("Task canceled"))
            return
        run = await self._cancelable_run(task_id)
        if run is None:
            raise TaskNotCancelableError(message="Task has no cancelable execution")
        self._coordinator.approvals.cancel_run(run)
        updater = TaskUpdater(event_queue, task_id, context_id)
        await updater.cancel(helpers.new_text_message("Task canceled"))
        await self._forget(task_id)

    async def cancel_durable_task(
        self, task_id: str, context: ServerCallContext
    ) -> Task | None:
        """Cancel durable ownership before consulting process-local SDK state."""

        task = await self._task_store.get(task_id, context)
        if task is None:
            return None
        if not await self._cancel_durable_task(
            task_id,
            context_id=task.context_id,
            user_id=context.user.user_name,
        ):
            return None
        cancelled = await self._persist_cancelled_task(context, task_id)
        await self._forget(task_id)
        return cancelled

    async def _persist_cancelled_task(
        self, context: ServerCallContext, task_id: str
    ) -> Task:
        """Persist durable cancellation when the SDK event queue is already closed."""

        task = await self._task_store.get(task_id, context)
        if task is None:
            raise TaskNotFoundError(message="Task not found")
        cancelled = Task()
        cancelled.CopyFrom(task)
        cancelled.status.CopyFrom(
            TaskStatus(
                state=TaskState.TASK_STATE_CANCELED,
                message=helpers.new_text_message("Task canceled"),
                timestamp=_timestamp_now(),
            )
        )
        await self._task_store.save(cancelled, context)
        return cancelled

    async def finalize_durable_cancellation(
        self, task_id: str, context: ServerCallContext
    ) -> Task | None:
        """Reassert cancellation after the SDK consumer drains stale events."""

        task = await self._task_store.get(task_id, context)
        if task is None:
            return None
        runtime = self._coordinator.external_continuations
        if runtime is None:
            return None
        try:
            kind, value = await runtime.response_boundary(
                response_id=task_id,
                user_id=context.user.user_name,
                session_id=task.context_id,
            )
        except KeyError:
            return None
        if kind != "durable_terminal" or value.status != "cancelled":
            return None
        return await self._persist_cancelled_task(context, task_id)

    async def refresh_task(
        self, task: Task, context: ServerCallContext
    ) -> Task:
        """Poll a durable external wait only when an A2A client asks for it."""

        if task.status.state in _TERMINAL_STATES:
            return task
        try:
            payload = await self._coordinator.poll_json(
                response_id=task.id,
                user_id=context.user.user_name,
                session_id=task.context_id,
            )
        except HTTPException as exc:
            # A regular live invocation has no durable run projection. It
            # remains owned by the official SDK event loop until it suspends.
            if exc.status_code == 404:
                return task
            raise
        status = payload.get("status")
        if status == "in_progress":
            return task
        if status == "requires_action" and not await self._can_resume_input(task.id):
            # Approval and client-tool stores are intentionally process-local.
            # A replica must not promise task-directed input it cannot deliver.
            payload = {**payload, "status": "failed"}
        refreshed = _task_from_poll_payload(task, payload)
        if refreshed.SerializeToString(deterministic=True) != task.SerializeToString(
            deterministic=True
        ):
            await self._task_store.save(refreshed, context)
        if refreshed.status.state in _TERMINAL_STATES:
            await self._forget(task.id)
        return refreshed

    async def _can_resume_input(self, task_id: str) -> bool:
        """Confirm this process owns the exact interactive continuation authority."""

        suspended = await self._suspended_execution(task_id)
        return suspended is not None and suspended.kind in {
            "approval",
            "client_tool",
        }

    async def _cancel_durable_task(
        self, task_id: str, *, context_id: str, user_id: str
    ) -> bool:
        """Delegate an awaited ``@task`` cancellation to its durable owner."""

        runtime = self._coordinator.external_continuations
        cancel = getattr(runtime, "cancel_task_wait", None)
        if not callable(cancel):
            return False
        return bool(
            await cancel(
                response_id=task_id,
                user_id=user_id,
                session_id=context_id,
            )
        )

    async def _execute_new(
        self,
        context: RequestContext,
        event_queue: EventQueue,
        principal: AuthPrincipal,
    ) -> None:
        """Choose direct-message or task execution from the requested semantics."""

        request = await self._invocation_request(context, principal.user_id)
        tracked = _requires_task(context)
        run = start_approval_run(
            self._coordinator.approvals,
            self._coordinator.client_tools,
            self._coordinator.driver,
            request,
            stream=tracked,
            external_continuations=self._coordinator.external_continuations,
        )
        if tracked:
            await self._remember_active(context.task_id, run)
        published = await _initial_task(context, event_queue) if tracked else None
        try:
            async with self._coordinator.semaphore:
                run.activation.set()
                await self._drain(
                    context,
                    event_queue,
                    run,
                    request,
                    principal,
                    published,
                )
        except asyncio.TimeoutError:
            self._coordinator.approvals.cancel_run(run)
            await self._fail(context, event_queue, published, "Agent timed out")
        finally:
            if not await self._is_suspended(context.task_id):
                await self._forget(context.task_id)

    async def _resume(
        self,
        context: RequestContext,
        event_queue: EventQueue,
        suspended: _SuspendedExecution,
    ) -> None:
        """Apply one caller response to the exact suspended Harnest execution."""

        _authorize_suspended(context, suspended)
        if suspended.kind == "external":
            raise UnsupportedOperationError(
                message="External work is resumed by polling, not a message"
            )
        published = await _initial_task(context, event_queue)
        await published.updater.start_work()
        await self._deliver_continuation(context, suspended)
        run = suspended.run
        if run is None:  # pragma: no cover - guarded by supported kinds
            raise UnsupportedOperationError(message="Task cannot be resumed")
        await self._remember_active(context.task_id, run)
        try:
            async with self._coordinator.semaphore:
                await self._drain(
                    context,
                    event_queue,
                    run,
                    suspended.request,
                    suspended.principal,
                    published,
                )
        except asyncio.TimeoutError:
            self._coordinator.approvals.cancel_run(run)
            await self._fail(context, event_queue, published, "Agent timed out")
        finally:
            if not await self._is_suspended(context.task_id):
                await self._forget(context.task_id)

    async def _deliver_continuation(
        self, context: RequestContext, suspended: _SuspendedExecution
    ) -> None:
        """Validate task-directed input before waking a protected operation."""

        message = _required_message(context)
        if suspended.kind == "approval":
            decision = _approval_decision(message)
            self._coordinator.approvals.decide(
                suspended.pending.id,
                user_id=suspended.request.user_id,
                decision=decision,
            )
            return
        if suspended.kind == "client_tool":
            await self._coordinator.client_tools.submit(
                suspended.pending.id,
                user_id=suspended.request.user_id,
                output=_continuation_value(message),
            )
            return
        raise UnsupportedOperationError(message="Task cannot accept this message")

    async def _drain(
        self,
        context: RequestContext,
        event_queue: EventQueue,
        run: ApprovalRun,
        request: InvocationRequest,
        principal: AuthPrincipal,
        published: _PublishedTask | None,
    ) -> None:
        """Project neutral events until the run completes or intentionally pauses."""

        deadline = asyncio.get_running_loop().time() + self._coordinator.request_timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            kind, value = await next_run_boundary(run, timeout=remaining)
            if kind == "event":
                if published is not None:
                    await _publish_runtime_event(published, value)
                continue
            if kind == "result":
                await self._complete(context, event_queue, published, value)
                return
            if kind in {"approval", "client_tool", "external_continuation"}:
                await self._suspend(
                    context,
                    event_queue,
                    published,
                    kind,
                    run,
                    value,
                    request,
                    principal,
                )
                return
            if kind == "error":
                await self._handle_run_error(context, event_queue, published, value)
                return
            raise RuntimeError(f"unexpected A2A run boundary: {kind}")

    async def _complete(
        self,
        context: RequestContext,
        event_queue: EventQueue,
        published: _PublishedTask | None,
        result: InvocationResult,
    ) -> None:
        """Return a direct Message unless task semantics were already activated."""

        require_customer_facing_output(result.text, result.result)
        if published is None:
            await event_queue.enqueue_event(
                _result_message(result, _required_identifier(context.context_id, "context"))
            )
            return
        await _publish_final_result(published, result)
        await published.updater.complete()

    async def _suspend(
        self,
        context: RequestContext,
        event_queue: EventQueue,
        published: _PublishedTask | None,
        kind: str,
        run: ApprovalRun,
        pending: Any,
        request: InvocationRequest,
        principal: AuthPrincipal,
    ) -> None:
        """Promote a direct interaction to a Task only when continuation is needed."""

        published = published or await _initial_task(context, event_queue)
        normalized = "external" if kind == "external_continuation" else kind
        await self._remember_suspended(
            _required_identifier(context.task_id, "task"),
            _SuspendedExecution(normalized, run, pending, request, principal),
        )
        if normalized == "approval":
            await published.updater.requires_input(
                _action_message("human_approval", pending.public(), published.task)
            )
        elif normalized == "client_tool":
            await published.updater.requires_input(
                _action_message("client_tool", pending.public(), published.task)
            )
        else:
            await published.updater.update_status(TaskState.TASK_STATE_WORKING)

    async def _handle_run_error(
        self,
        context: RequestContext,
        event_queue: EventQueue,
        published: _PublishedTask | None,
        error: BaseException,
    ) -> None:
        """Map protected denials separately while keeping other failures private."""

        if isinstance(error, asyncio.CancelledError):
            raise error
        published = published or await _initial_task(context, event_queue)
        if isinstance(error, ApprovalDenied):
            await published.updater.reject(helpers.new_text_message("Approval denied"))
            return
        await published.updater.failed(helpers.new_text_message("Agent execution failed"))

    async def _fail(
        self,
        context: RequestContext,
        event_queue: EventQueue,
        published: _PublishedTask | None,
        message: str,
    ) -> None:
        """Publish a protocol-valid failed Task without exposing an exception chain."""

        published = published or await _initial_task(context, event_queue)
        await published.updater.failed(helpers.new_text_message(message))

    async def _invocation_request(
        self, context: RequestContext, user_id: str
    ) -> InvocationRequest:
        """Authorize the A2A context as a Harnest session and validate its content."""

        message = _required_message(context)
        context_id = _required_identifier(context.context_id, "context")
        await self._ensure_session(context, context_id, user_id)
        value = _message_input(message, self._coordinator.driver.info.input_schema)
        resolved = await self._coordinator.resolve_input(
            value,
            user_id=user_id,
            session_id=context_id,
            require_non_empty_text=True,
        )
        return self._coordinator.create_request(
            resolved,
            user_id=user_id,
            session_id=context_id,
            invocation_id=_required_identifier(context.task_id, "task"),
            metadata=context.metadata,
            transport="a2a_stream" if _is_streaming(context) else "a2a",
        )

    async def _ensure_session(
        self, context: RequestContext, context_id: str, user_id: str
    ) -> None:
        """Create only a new conversation context; existing tasks require ownership."""

        session = await self._coordinator.driver.get_session(
            session_id=context_id, user_id=user_id
        )
        if session is not None:
            return
        if context.current_task is not None:
            raise TaskNotFoundError(message="Task context is unavailable")
        try:
            await self._coordinator.driver.create_session(
                session_id=context_id, user_id=user_id, state={}
            )
        except SessionConflictError as exc:
            # A cross-principal collision is indistinguishable from absence.
            raise TaskNotFoundError(message="Task context is unavailable") from exc

    async def _remember_active(self, task_id: str | None, run: ApprovalRun) -> None:
        """Register a run only after the caller selected task semantics."""

        async with self._lock:
            self._active[_required_identifier(task_id, "task")] = run

    async def _remember_suspended(
        self, task_id: str, execution: _SuspendedExecution
    ) -> None:
        """Replace active ownership with one explicit resumable boundary."""

        async with self._lock:
            self._active.pop(task_id, None)
            self._suspended[task_id] = execution

    async def _suspended_execution(
        self, task_id: str | None
    ) -> _SuspendedExecution | None:
        """Read a process-local continuation without exposing it in Task metadata."""

        if not task_id:
            return None
        async with self._lock:
            return self._suspended.get(task_id)

    async def _cancelable_run(self, task_id: str) -> ApprovalRun | None:
        """Resolve only live Python work; external provider waits are not cancelable."""

        async with self._lock:
            run = self._active.get(task_id)
            if run is not None:
                return run
            suspended = self._suspended.get(task_id)
            if suspended is None or suspended.kind == "external":
                return None
            return suspended.run

    async def _is_suspended(self, task_id: str | None) -> bool:
        """Keep a continuation registered when its executor method returns."""

        if not task_id:
            return False
        async with self._lock:
            return task_id in self._suspended

    async def _forget(self, task_id: str | None) -> None:
        """Drop process-local execution references after terminal publication."""

        if not task_id:
            return
        async with self._lock:
            self._active.pop(task_id, None)
            self._suspended.pop(task_id, None)


class _HarnestRequestHandler(DefaultRequestHandler):
    """Annotate official handler calls with transport intent for minimum task use."""

    def __init__(self, executor: HarnestA2AExecutor, **kwargs: Any) -> None:
        super().__init__(agent_executor=executor, **kwargs)
        self._harnest_executor = executor

    async def on_message_send(self, params: Any, context: ServerCallContext) -> Any:
        """Mark blocking sends so the executor may return a direct Message."""

        await self._authorize_task_reference(params.message.task_id, context)
        context.state[_STREAMING_STATE_KEY] = False
        return await super().on_message_send(params, context)

    async def on_message_send_stream(
        self, params: Any, context: ServerCallContext
    ) -> AsyncIterator[Any]:
        """Mark streaming sends before the official handler starts execution."""

        await self._authorize_task_reference(params.message.task_id, context)
        context.state[_STREAMING_STATE_KEY] = True
        async for event in super().on_message_send_stream(params, context):
            yield event

    async def on_get_task(self, params: Any, context: ServerCallContext) -> Any:
        """Refresh durable external work only for the singular task requested."""

        task = await self.task_store.get(params.id, context)
        if task is not None:
            await self._harnest_executor.refresh_task(task, context)
        return await super().on_get_task(params, context)

    async def on_cancel_task(self, params: Any, context: ServerCallContext) -> Any:
        """Prefer durable ownership over the SDK's process-local task registry."""

        await self._require_owned_task(params.id, context)
        # A durable wait outlives the SDK producer that created it. Once that
        # producer is cleaned up, the SDK registry cannot route cancellation,
        # while Harnest still owns the queued task and continuation records.
        durable = await self._harnest_executor.cancel_durable_task(
            params.id, context
        )
        if durable is not None:
            return durable
        result = await super().on_cancel_task(params, context)
        finalized = await self._harnest_executor.finalize_durable_cancellation(
            params.id, context
        )
        return result if finalized is None else finalized

    async def on_subscribe_to_task(
        self, params: Any, context: ServerCallContext
    ) -> AsyncIterator[Any]:
        """Merge official live events with explicit durable status polling."""

        await self._require_owned_task(params.id, context)
        source = super().on_subscribe_to_task(params, context)
        terminal_refresh = False
        try:
            async for event, refreshed_terminal in _subscription_events(
                source,
                task_id=params.id,
                task_store=self.task_store,
                executor=self._harnest_executor,
                context=context,
            ):
                terminal_refresh = terminal_refresh or refreshed_terminal
                yield event
        finally:
            if terminal_refresh:
                # A stateless durable completion cannot close the SDK queue,
                # so release the idle producer after its terminal snapshot.
                active = await self._active_task_registry.get(params.id)
                if active is not None:
                    await active.aclose()

    async def _authorize_task_reference(
        self, task_id: str, context: ServerCallContext
    ) -> None:
        """Preflight task-directed messages before the SDK's global registry."""

        if task_id:
            await self._require_owned_task(task_id, context)

    async def _require_owned_task(
        self, task_id: str, context: ServerCallContext
    ) -> Task:
        """Hide another principal's active registry entry as an absent task."""

        task = await self.task_store.get(task_id, context)
        if task is None:
            raise TaskNotFoundError(message="Task not found")
        return task


async def _subscription_events(
    source: AsyncIterator[Any],
    *,
    task_id: str,
    task_store: TaskStore,
    executor: HarnestA2AExecutor,
    context: ServerCallContext,
) -> AsyncIterator[tuple[Any, bool]]:
    """Yield live events while checking shared durable state at bounded intervals."""

    pending = asyncio.create_task(anext(source))
    last_snapshot: bytes | None = None
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=0.25)
            if pending in done:
                try:
                    event = pending.result()
                except StopAsyncIteration:
                    return
                if isinstance(event, Task):
                    last_snapshot = event.SerializeToString(deterministic=True)
                yield event, False
                pending = asyncio.create_task(anext(source))
                continue
            task = await task_store.get(task_id, context)
            if task is None:
                raise TaskNotFoundError(message="Task not found")
            refreshed = await executor.refresh_task(task, context)
            snapshot = refreshed.SerializeToString(deterministic=True)
            if snapshot != last_snapshot:
                last_snapshot = snapshot
                terminal = refreshed.status.state in _TERMINAL_STATES
                yield refreshed, terminal
                if terminal:
                    return
    finally:
        if not pending.done():
            pending.cancel()
        with suppress(asyncio.CancelledError, StopAsyncIteration):
            await pending
        await source.aclose()


def mount_a2a_routes(
    router: Any,
    *,
    driver: Any,
    coordinator: InvocationCoordinator,
    task_store: TaskStore | None = None,
) -> None:
    """Mount only the A2A bindings explicitly declared by the compiled card."""

    card = _agent_card(driver.info.card)
    _validate_server_card(card)
    bindings = _declared_bindings(card)
    if not bindings:
        return
    store = task_store or InMemoryTaskStore()
    if not isinstance(store, TaskStore):
        raise TypeError("a2a_task_store must implement the A2A TaskStore interface")
    executor = HarnestA2AExecutor(coordinator, store)
    handler = _HarnestRequestHandler(
        executor,
        task_store=store,
        agent_card=card,
    )
    context_builder: ServerCallContextBuilder = _HarnestCallContextBuilder()
    for binding, path, compat in bindings:
        routes = (
            create_jsonrpc_routes(
                handler,
                rpc_url=path,
                context_builder=context_builder,
                enable_v0_3_compat=compat,
            )
            if binding == "jsonrpc"
            else create_rest_routes(
                handler,
                context_builder=context_builder,
                enable_v0_3_compat=compat,
                path_prefix="" if path == "/" else path,
            )
        )
        router.routes.extend(routes)


def _agent_card(value: Mapping[str, Any]) -> AgentCard:
    """Parse the already schema-validated authoring card into normative types."""

    try:
        return json_format.ParseDict(dict(value), AgentCard())
    except (TypeError, ValueError) as exc:
        raise ValueError("agent-card.yaml is not a valid A2A 1.x Agent Card") from exc


def _declared_bindings(card: AgentCard) -> tuple[tuple[str, str, bool], ...]:
    """Normalize route paths and coalesce compatibility declarations."""

    declared: dict[tuple[str, str], set[str]] = {}
    for interface in card.supported_interfaces:
        binding = _binding_name(interface.protocol_binding)
        if binding is None:
            continue
        path = urlsplit(interface.url).path or "/"
        normalized = path.rstrip("/") or "/"
        declared.setdefault((binding, normalized), set()).add(
            interface.protocol_version
        )
    return tuple(
        (binding, path, "0.3" in versions)
        for (binding, path), versions in declared.items()
    )


def _validate_server_card(card: AgentCard) -> None:
    """Reject declarations that the Harnest HTTP host cannot fulfill."""

    for interface in card.supported_interfaces:
        binding = interface.protocol_binding.upper().replace("-", "").replace("_", "")
        if binding == "GRPC":
            raise ValueError("Harnest A2A serving does not yet support gRPC bindings")
        if _binding_name(interface.protocol_binding) is not None and interface.protocol_version not in {"0.3", "1.0"}:
            raise ValueError(
                f"Harnest A2A serving does not support protocol version {interface.protocol_version!r}"
            )
    if card.capabilities.push_notifications:
        raise ValueError("Harnest A2A serving does not yet support push notifications")
    if card.capabilities.extended_agent_card:
        raise ValueError("Harnest A2A serving does not yet support extended Agent Cards")
    if card.capabilities.extensions:
        raise ValueError("Harnest A2A serving does not yet support A2A extensions")


def _binding_name(value: str) -> str | None:
    """Recognize core HTTP bindings without claiming gRPC or custom support."""

    normalized = value.upper().replace("-", "").replace("_", "")
    if normalized == "JSONRPC":
        return "jsonrpc"
    if normalized in {"HTTP+JSON", "HTTPJSON", "REST"}:
        return "rest"
    return None


def _request_principal(context: ServerCallContext) -> AuthPrincipal:
    """Require the principal inserted at the authenticated route boundary."""

    principal = context.state.get(_PRINCIPAL_STATE_KEY)
    if not isinstance(principal, AuthPrincipal):
        raise RuntimeError("A2A request is missing Harnest principal context")
    return principal


def _required_message(context: RequestContext) -> Message:
    """Require an A2A user message before touching Harnest session state."""

    message = context.message
    if message is None or message.role != Role.ROLE_USER or not message.parts:
        raise ContentTypeNotSupportedError(message="A user message is required")
    return message


def _required_identifier(value: str | None, kind: str) -> str:
    """Reject missing SDK-generated identities before persistence."""

    if not value:
        raise RuntimeError(f"A2A {kind} ID is unavailable")
    return value


def _message_input(message: Message, input_schema: Any) -> Any:
    """Accept text for plain agents and one structured DataPart for typed agents."""

    kinds = [part.WhichOneof("content") for part in message.parts]
    if input_schema is None:
        if any(kind != "text" for kind in kinds):
            raise ContentTypeNotSupportedError(
                message="This agent accepts text parts only"
            )
        text = "\n".join(part.text for part in message.parts)
        if not text.strip():
            raise ContentTypeNotSupportedError(message="Message text is empty")
        return text
    if len(message.parts) != 1 or kinds != ["data"]:
        raise ContentTypeNotSupportedError(
            message="This agent requires one structured data part"
        )
    return json_format.MessageToDict(message.parts[0].data)


def _requires_task(context: RequestContext) -> bool:
    """Create task state only for semantics that cannot use a direct Message."""

    configuration = context.configuration
    return bool(
        context.current_task is not None
        or _is_streaming(context)
        or (configuration is not None and configuration.return_immediately)
    )


def _is_streaming(context: RequestContext) -> bool:
    """Read transport intent set by the binding-neutral request handler."""

    return context.call_context.state.get(_STREAMING_STATE_KEY) is True


async def _initial_task(
    context: RequestContext, event_queue: EventQueue
) -> _PublishedTask:
    """Publish the Task first, as required for every tracked A2A stream."""

    message = _required_message(context)
    if context.current_task is None:
        task = helpers.new_task_from_user_message(message)
    else:
        # A continuation starts new work. Publishing its previous interrupted
        # state first would make the official blocking aggregator stop before
        # it observes the resumed execution.
        task = Task()
        task.CopyFrom(context.current_task)
        task.status.CopyFrom(
            TaskStatus(
                state=TaskState.TASK_STATE_WORKING,
                timestamp=_timestamp_now(),
            )
        )
    await event_queue.enqueue_event(task)
    updater = TaskUpdater(event_queue, task.id, task.context_id)
    if context.current_task is None:
        await updater.start_work()
    return _PublishedTask(task, updater)


async def _publish_runtime_event(
    published: _PublishedTask, event: Mapping[str, Any]
) -> None:
    """Project answer and agent activity through A2A without exposing tool data."""

    event_type = event.get("type")
    if event_type == "thinking":
        text = event.get("text")
        if isinstance(text, str) and text:
            await published.updater.update_status(
                TaskState.TASK_STATE_WORKING,
                helpers.new_text_message(text, role=Role.ROLE_AGENT),
                metadata=_a2a_activity_metadata(event),
            )
        return
    if event_type in {"agent_activity", "agent_metadata"}:
        await published.updater.update_status(
            TaskState.TASK_STATE_WORKING,
            metadata=_a2a_activity_metadata(event),
        )
        return
    if event_type != "message":
        # Tool arguments and results remain inside the executing agent boundary.
        return
    text = event.get("text")
    if not isinstance(text, str) or not text:
        return
    await published.updater.add_artifact(
        [helpers.new_text_part(text, media_type="text/plain")],
        artifact_id=f"{published.task.id}-response",
        name="response",
        metadata=_a2a_message_metadata(event),
        append=published.text_started,
    )
    published.text_started = True


def _a2a_activity_metadata(event: Mapping[str, Any]) -> dict[str, Any]:
    """Build the namespaced A2A extension metadata for portable activity."""

    if event.get("type") == "agent_metadata":
        return {
            "harnest": {
                "type": "agent_metadata",
                **_a2a_agent(event),
                **_agent_metadata_from_runtime_event(event).as_dict(),
            }
        }
    activity: dict[str, Any] = {"type": event.get("type")}
    for key in ("agent", "activity", "target", "code"):
        value = event.get(key)
        if isinstance(value, str) and value:
            activity[key] = value
    return {"harnest": activity}


def _a2a_agent(event: Mapping[str, Any]) -> dict[str, str]:
    """Return an optional validated agent label for A2A extension metadata."""

    agent = event.get("agent")
    return {"agent": agent} if isinstance(agent, str) and agent else {}


def _a2a_message_metadata(event: Mapping[str, Any]) -> dict[str, Any] | None:
    """Attribute an A2A answer artifact when the framework named its agent."""

    agent = event.get("agent")
    if not isinstance(agent, str) or not agent:
        return None
    return {"harnest": {"agent": agent}}


async def _publish_final_result(
    published: _PublishedTask, result: InvocationResult
) -> None:
    """Publish only content not already emitted by neutral stream events."""

    parts: list[Part] = []
    if result.text and not published.text_started:
        parts.append(helpers.new_text_part(result.text, media_type="text/plain"))
    if result.result is not None:
        parts.append(helpers.new_data_part(result.result, media_type="application/json"))
    metadata = _a2a_result_metadata(result)
    if not parts and metadata is None:
        return
    await published.updater.add_artifact(
        parts,
        artifact_id=f"{published.task.id}-response",
        name="response",
        metadata=metadata,
        append=published.text_started,
        last_chunk=True,
    )


def _result_message(result: InvocationResult, context_id: str) -> Message:
    """Build the minimum direct response without manufacturing a Task."""

    parts: list[Part] = []
    if result.text:
        parts.append(helpers.new_text_part(result.text, media_type="text/plain"))
    if result.result is not None:
        parts.append(helpers.new_data_part(result.result, media_type="application/json"))
    message = helpers.new_message(
        parts=parts, context_id=context_id, role=Role.ROLE_AGENT
    )
    metadata = _a2a_result_metadata(result)
    if metadata is not None:
        json_format.ParseDict(metadata, message.metadata)
    return message


def _a2a_result_metadata(result: InvocationResult) -> dict[str, Any] | None:
    """Persist aggregate usage without copying per-call raw provider metadata."""

    usage = _aggregate_token_usage(result.events)
    return {"harnest": {"usage": usage.as_dict()}} if usage is not None else None


def _action_message(kind: str, value: Mapping[str, Any], task: Task) -> Message:
    """Represent Harnest continuations as explicit structured A2A input requests."""

    return helpers.new_data_message(
        {"type": kind, **dict(value)},
        media_type="application/json",
        context_id=task.context_id,
        task_id=task.id,
    )


def _approval_decision(message: Message) -> str:
    """Accept a narrow decision value without treating arbitrary text as authority."""

    value = _continuation_value(message)
    decision = value.get("decision") if isinstance(value, Mapping) else value
    if isinstance(decision, str) and decision.strip().lower() in {"approve", "deny"}:
        return decision.strip().lower()
    raise ContentTypeNotSupportedError(
        message="Approval response must be approve or deny"
    )


def _continuation_value(message: Message) -> Any:
    """Decode one text or structured continuation response."""

    if len(message.parts) != 1:
        raise ContentTypeNotSupportedError(
            message="Continuation responses require exactly one part"
        )
    part = message.parts[0]
    kind = part.WhichOneof("content")
    if kind == "text":
        return part.text
    if kind == "data":
        return json_format.MessageToDict(part.data)
    raise ContentTypeNotSupportedError(
        message="Continuation response must contain text or structured data"
    )


def _authorize_suspended(
    context: RequestContext, suspended: _SuspendedExecution
) -> None:
    """Bind continuation delivery to the original user and conversation."""

    if context.call_context.user.user_name != suspended.request.user_id:
        raise TaskNotFoundError(message="Task not found")
    if context.context_id != suspended.request.session_id:
        raise TaskNotFoundError(message="Task not found")


def _task_from_poll_payload(task: Task, payload: Mapping[str, Any]) -> Task:
    """Project an on-demand continuation poll into a persisted A2A snapshot."""

    refreshed = Task()
    refreshed.CopyFrom(task)
    status = payload.get("status")
    state = _poll_task_state(status)
    if state is None:
        return refreshed
    task_status = TaskStatus(state=state, timestamp=_timestamp_now())
    if state == TaskState.TASK_STATE_FAILED:
        task_status.message.CopyFrom(
            helpers.new_text_message(
                "Agent execution failed",
                context_id=task.context_id,
                task_id=task.id,
            )
        )
    elif state == TaskState.TASK_STATE_INPUT_REQUIRED:
        action = payload.get("requiredAction")
        if isinstance(action, Mapping):
            task_status.message.CopyFrom(
                helpers.new_data_message(
                    dict(action),
                    media_type="application/json",
                    context_id=task.context_id,
                    task_id=task.id,
                )
            )
    refreshed.status.CopyFrom(task_status)
    if state == TaskState.TASK_STATE_COMPLETED and not _has_response_artifact(task):
        parts = _poll_result_parts(payload)
        if parts:
            artifact = helpers.new_artifact(
                parts, name="response", artifact_id=f"{task.id}-response"
            )
            usage = payload.get("usage")
            if isinstance(usage, Mapping):
                json_format.ParseDict(
                    {"harnest": {"usage": dict(usage)}}, artifact.metadata
                )
            refreshed.artifacts.append(artifact)
    return refreshed


def _poll_task_state(status: Any) -> int | None:
    """Map only known neutral response states into normative A2A states."""

    return {
        "completed": TaskState.TASK_STATE_COMPLETED,
        "failed": TaskState.TASK_STATE_FAILED,
        "requires_action": TaskState.TASK_STATE_INPUT_REQUIRED,
    }.get(status)


def _has_response_artifact(task: Task) -> bool:
    """Keep repeated and concurrent durable refreshes artifact-idempotent."""

    expected = f"{task.id}-response"
    return any(artifact.artifact_id == expected for artifact in task.artifacts)


def _poll_result_parts(payload: Mapping[str, Any]) -> list[Part]:
    """Include only terminal customer output from a continuation poll."""

    parts: list[Part] = []
    text = payload.get("outputText")
    if isinstance(text, str) and text:
        parts.append(helpers.new_text_part(text, media_type="text/plain"))
    if payload.get("result") is not None:
        parts.append(
            helpers.new_data_part(payload["result"], media_type="application/json")
        )
    return parts


def _timestamp_now() -> Timestamp:
    """Create a timezone-aware Proto timestamp for lazy task refreshes."""

    value = Timestamp()
    value.FromDatetime(datetime.now(timezone.utc))
    return value


__all__ = ["HarnestA2AExecutor", "mount_a2a_routes"]
