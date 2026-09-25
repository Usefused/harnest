"""Typed tools whose implementation runs in the connected client."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import TYPE_CHECKING, Any, Iterator, TypeVar, overload

from .assets import AssetScope, AssetStorage, AssetStoreError
from .logging import get_logger
from .structured import (
    PydanticModel,
    callable_output_schema,
    validate_output_schema,
    validate_output_value,
)
from .transient_media import (
    TransientMediaAccess,
    TransientMediaError,
    TransientMediaLeaseStore,
    TransientMediaScope,
    stage_transient_media_batch,
)
from .stored_media import (
    StoredMediaError,
    StoredMediaStage,
    stage_stored_media_transaction,
)

if TYPE_CHECKING:
    from .agent.approval import ApprovalRun

F = TypeVar("F", bound=Callable[..., Any])
_CURRENT: contextvars.ContextVar["ClientToolExecution | None"] = contextvars.ContextVar(
    "harnest_client_tool_execution", default=None
)
_AUDIT = get_logger("client_tool.audit")


class ClientToolError(RuntimeError):
    """A client tool request could not be completed safely."""


@dataclass(frozen=True, slots=True)
class _StagedClientToolResult:
    """Carry sanitized JSON past the wrapper's ordinary validation path."""

    value: Any = field(repr=False)
    lease_ids: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class _PreparedClientToolResult:
    """Keep a durable rollback handle until the future commit succeeds."""

    value: Any = field(repr=False)
    durable: StoredMediaStage | None = field(default=None, repr=False)


@dataclass(slots=True)
class PendingClientTool:
    """One principal-scoped client tool call awaiting its result."""

    id: str
    user_id: str
    session_id: str
    call_id: str
    name: str
    arguments: dict[str, Any]
    output_schema: PydanticModel | None
    expires_at: float
    run: ApprovalRun = field(repr=False)
    future: asyncio.Future[Any] = field(repr=False)
    submitting: bool = field(default=False, repr=False)
    agui_tool_call_id: str | None = field(default=None, repr=False)
    private_input: bool = False

    def public(self) -> dict[str, Any]:
        """Return the client-safe fields required to execute this tool call."""

        payload = {
            "id": self.id,
            "callId": self.call_id,
            "name": self.name,
            "arguments": dict(self.arguments),
            "expiresAt": datetime.fromtimestamp(
                self.expires_at, tz=timezone.utc
            ).isoformat(),
        }
        if self.private_input:
            payload["privateInput"] = True
            payload["inputSchema"] = self.output_schema.model_json_schema()
        return payload


class InMemoryClientToolStore:
    """Development store for one-time client tool result delivery."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        transient_media: TransientMediaLeaseStore | None = None,
        asset_stores: Mapping[str, AssetStorage] | None = None,
    ) -> None:
        """Configure one-time continuations and their private media leases."""

        self._clock = clock
        self._items: dict[str, PendingClientTool] = {}
        self._lock = Lock()
        self._transient_media = transient_media or TransientMediaLeaseStore()
        self._asset_stores = dict(asset_stores or {})
        self._cleanup_registered: set[TransientMediaScope] = set()

    @property
    def transient_media(self) -> TransientMediaLeaseStore:
        """Expose the lease boundary to framework adapters, never to clients."""

        return self._transient_media

    async def suspend(
        self,
        run: ApprovalRun,
        *,
        name: str,
        arguments: dict[str, Any],
        output_schema: PydanticModel | None,
        timeout_seconds: int,
        private_input: bool = False,
    ) -> Any:
        """Suspend for one result, transferring private values without retention."""

        if private_input and output_schema is None:
            raise ClientToolError("private client input requires a schema")
        loop = asyncio.get_running_loop()
        pending = PendingClientTool(
            id=f"client_tool_{uuid.uuid4().hex}",
            user_id=run.user_id,
            session_id=run.session_id,
            call_id=run.call_id,
            name=name,
            arguments=arguments,
            output_schema=output_schema,
            expires_at=self._clock() + timeout_seconds,
            run=run,
            future=loop.create_future(),
            private_input=private_input,
        )
        with self._lock:
            self._items[pending.id] = pending
        _audit(name, "requested", trigger="agent", outcome="suspended")
        run.notifications.put_nowait(("client_tool", pending))
        try:
            result = await asyncio.wait_for(pending.future, timeout=timeout_seconds)
            if private_input:
                # Python 3.11 wait_for can return a completed future even when
                # this task was cancelled. Never transfer private input then.
                owner = asyncio.current_task()
                if owner is not None and owner.cancelling():
                    raise asyncio.CancelledError
                return result.take()
            return result
        except asyncio.TimeoutError as exc:
            _audit(name, "expired", trigger="agent", outcome="failed")
            raise ClientToolError(f"client tool {name!r} timed out") from exc
        finally:
            with self._lock:
                self._items.pop(pending.id, None)
            _clear_private_delivery(pending)

    async def submit(
        self, request_id: str, *, user_id: str, output: Any
    ) -> PendingClientTool:
        """Validate and sanitize a client result before its framework resumes."""

        pending = self._reserve_submission(request_id, user_id)
        try:
            pending.run._validate_resume_authority()
            prepared = await self._prepared_output(pending, output)
            await self._commit_output(pending, prepared)
        except (
            ValueError,
            PermissionError,
            AssetStoreError,
            StoredMediaError,
            TransientMediaError,
        ) as exc:
            self._release_submission(pending)
            raise ClientToolError(str(exc)) from exc
        except BaseException:
            self._release_submission(pending)
            raise
        _audit(
            pending.name,
            "result_submitted",
            trigger="user",
            outcome="committed",
        )
        return pending

    async def _commit_output(
        self, pending: PendingClientTool, prepared: _PreparedClientToolResult
    ) -> None:
        """Commit validated output and authority together, rolling back on rejection."""

        try:
            with self._lock:
                if pending.future.done():
                    raise ClientToolError("client tool result was already submitted")
                if self._clock() >= pending.expires_at:
                    raise ClientToolError("client tool request is expired")
                # Preparation can yield. Recheck authority only after output is
                # valid, with no await between accepting it and waking the run.
                pending.run._accept_resume_authority()
                pending.future.set_result(prepared.value)
                pending.submitting = False
        except BaseException:
            # Remove only this rejected submission's leases; parallel accepted
            # results may still need other media owned by the same run.
            if pending.private_input:
                prepared.value.clear()
            if isinstance(prepared.value, _StagedClientToolResult):
                self._transient_media.commit(
                    scope=_transient_scope(pending), lease_ids=prepared.value.lease_ids
                )
            if prepared.durable is not None:
                await prepared.durable.rollback(
                    scope=AssetScope(pending.user_id, pending.session_id)
                )
            raise

    def _reserve_submission(
        self, request_id: str, user_id: str
    ) -> PendingClientTool:
        """Atomically grant one caller the right to cross async validation."""

        with self._lock:
            pending = self._items.get(request_id)
            if pending is None or pending.user_id != user_id:
                raise KeyError("client tool request not found")
            if self._clock() >= pending.expires_at:
                raise ClientToolError("client tool request is expired")
            if pending.future.done() or pending.submitting:
                raise ClientToolError("client tool result was already submitted")
            pending.submitting = True
            return pending

    def _release_submission(self, pending: PendingClientTool) -> None:
        """Allow a clean retry after validation or storage failed."""

        with self._lock:
            pending.submitting = False

    async def _prepared_output(
        self, pending: PendingClientTool, output: Any
    ) -> _PreparedClientToolResult:
        """Keep private input out of durable assets and model media staging."""

        if pending.private_input:
            from .client_input import _prepare_private_input

            return _PreparedClientToolResult(_prepare_private_input(pending.output_schema, output))
        if pending.output_schema is None:
            return _PreparedClientToolResult(output)
        validated = validate_output_value(
            pending.output_schema,
            output,
            boundary=f"client tool {pending.name!r} output",
        )
        scope = _transient_scope(pending)
        durable = await stage_stored_media_transaction(
            validated,
            stores=self._asset_stores,
            scope=AssetScope(pending.user_id, pending.session_id),
        )
        try:
            value, lease_ids = stage_transient_media_batch(
                durable.value,
                store=self._transient_media,
                scope=scope,
            )
            result = _StagedClientToolResult(value, lease_ids)
        except BaseException:
            await durable.rollback(
                scope=AssetScope(pending.user_id, pending.session_id)
            )
            raise
        self._register_cleanup(pending, scope)
        return _PreparedClientToolResult(result, durable)

    def _register_cleanup(
        self, pending: PendingClientTool, scope: TransientMediaScope
    ) -> None:
        """Force-clear leases when completion, failure, or cancellation ends the run."""

        task = pending.run.task
        if task is None or scope in self._cleanup_registered:
            return
        self._cleanup_registered.add(scope)

        def cleanup(_task: asyncio.Task[Any]) -> None:
            # The model adapter normally commits after success; this terminal
            # guard owns every exceptional and disconnected path.
            self._transient_media.clear(scope=scope)
            self._cleanup_registered.discard(scope)

        task.add_done_callback(cleanup)

    def run_for(self, pending: PendingClientTool) -> ApprovalRun:
        """Return the live invocation waiting for a client tool result."""

        return pending.run

    def pending_for(self, *, user_id: str, session_id: str) -> list[PendingClientTool]:
        """Resolve live client calls without revealing another session's work."""

        with self._lock:
            return [
                pending for pending in self._items.values()
                if pending.user_id == user_id and pending.session_id == session_id
                and not pending.future.done()
            ]

    def cancel(self, request_id: str, *, user_id: str) -> PendingClientTool:
        """Deliver a caller cancellation through the same one-time authority gate."""

        pending = self._reserve_submission(request_id, user_id)
        try:
            pending.run._accept_resume_authority()
            pending.future.set_exception(ClientToolError("client tool was cancelled"))
        finally:
            self._release_submission(pending)
        _audit(pending.name, "cancelled", trigger="user", outcome="cancelled")
        return pending


@dataclass(frozen=True, slots=True)
class ClientToolExecution:
    """Runtime capability visible only while Harnest owns an invocation."""

    store: InMemoryClientToolStore
    run: ApprovalRun

    async def execute(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        output_schema: PydanticModel | None,
        timeout_seconds: int,
        private_input: bool = False,
    ) -> Any:
        """Request one client-hosted tool call and await its validated result."""

        return await self.store.suspend(
            self.run,
            name=name,
            arguments=arguments,
            output_schema=output_schema,
            timeout_seconds=timeout_seconds,
            private_input=private_input,
        )

    @property
    def transient_media(self) -> TransientMediaAccess:
        """Return private media access already scoped to this invocation."""

        return TransientMediaAccess(
            store=self.store.transient_media,
            scope=TransientMediaScope(
                self.run.user_id,
                self.run.session_id,
                self.run.call_id,
            ),
        )


@contextmanager
def client_tool_execution(execution: ClientToolExecution) -> Iterator[None]:
    """Activate client-tool execution authority for the current context."""

    token = _CURRENT.set(execution)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current_transient_media() -> TransientMediaAccess | None:
    """Expose scoped staged bytes only during a managed invocation."""

    execution = _CURRENT.get()
    return execution.transient_media if isinstance(execution, ClientToolExecution) else None


@overload
def client_tool(function: F) -> F: ...


@overload
def client_tool(
    *,
    description: str | None = None,
    timeout_seconds: int = 300,
    output_schema: PydanticModel | None = None,
    permission: str | None = None,
) -> Callable[[F], F]: ...


def client_tool(
    function: F | None = None,
    *,
    description: str | None = None,
    timeout_seconds: int = 300,
    output_schema: PydanticModel | None = None,
    permission: str | None = None,
) -> F | Callable[[F], F]:
    """Declare a client tool with an optional runtime-principal permission tag."""

    if not isinstance(timeout_seconds, int) or timeout_seconds < 1:
        raise ValueError("client tool timeout_seconds must be a positive integer")
    if permission is not None:
        from .agent_principal import validate_permission

        validate_permission(permission)
    configured_schema = validate_output_schema(
        output_schema, field_name="client tool output_schema"
    )

    def decorate(fn: F) -> F:
        """Reject private input consumers before ordinary model-result wrapping."""

        if getattr(fn, "__harnest_client_input__", False):
            raise TypeError("private client input cannot also be a client tool")
        schema = configured_schema or callable_output_schema(fn)
        from .context import registration_for as context_registration_for
        from .lifecycle import registration_for as lifecycle_registration_for

        if context_registration_for(fn) is not None:
            raise TypeError("context providers cannot also be client tools")
        if lifecycle_registration_for(fn) is not None:
            raise TypeError("lifecycle extensions cannot also be client tools")
        if description:
            fn.__doc__ = description
        if not (inspect.getdoc(fn) or "").strip():
            raise ValueError("client tool needs a docstring or description")

        @functools.wraps(fn)
        async def invoke(*args: Any, **kwargs: Any) -> Any:
            from .agent.approval import bind_tool_arguments
            from .agent_principal import require_capability

            require_capability(invoke, name=fn.__name__)
            execution = _CURRENT.get()
            if execution is None:
                raise ClientToolError(
                    f"client tool {fn.__name__!r} requires the managed Harnest runtime"
                )
            arguments = bind_tool_arguments(fn, args, kwargs)
            output = await execution.execute(
                name=fn.__name__,
                arguments=arguments,
                output_schema=schema,
                timeout_seconds=timeout_seconds,
            )
            if isinstance(output, _StagedClientToolResult):
                # Submission happens in the transport task. Establish private
                # correlation only when the owning framework branch resumes.
                execution.transient_media.bind(output.lease_ids)
                return output.value
            if schema is None:
                return output
            return validate_output_value(
                schema, output, boundary=f"client tool {fn.__name__!r} output"
            )

        setattr(invoke, "__harnest_tool__", True)
        setattr(invoke, "__harnest_client_tool__", True)
        if schema is not None:
            invoke.__annotations__ = {
                **getattr(fn, "__annotations__", {}),
                "return": schema,
            }
            invoke.__signature__ = inspect.signature(fn).replace(  # type: ignore[attr-defined]
                return_annotation=schema
            )
            setattr(invoke, "__harnest_output_schema__", schema)
        # Approval can sit above or below @client_tool. Resolve the wrapper only
        # after the client-tool marker exists so both authored orders are safe.
        from .agent.approval import wrap_approved_tool

        governed = wrap_approved_tool(invoke)
        if permission is not None:
            from .agent_principal import attach_required_permissions

            attach_required_permissions(invoke, (permission,))
            attach_required_permissions(governed, (permission,))
        return governed  # type: ignore[return-value]

    return decorate(function) if function is not None else decorate


def _audit(name: str, operation: str, *, trigger: str, outcome: str) -> None:
    # Arguments and client results can contain customer data, so the audit
    # boundary records only the stable declared capability name.
    _AUDIT.info(
        f"client_tool.{operation}",
        operation=operation,
        trigger=trigger,
        outcome=outcome,
        action=f"client_tool:{name}",
    )


def _clear_private_delivery(pending: PendingClientTool) -> None:
    """Clear values left in a completed future, including cancellation races."""

    if not pending.private_input or not pending.future.done() or pending.future.cancelled():
        return
    if pending.future.exception() is None:
        pending.future.result().clear()


def _transient_scope(pending: PendingClientTool) -> TransientMediaScope:
    """Derive lease ownership only from the authenticated pending request."""

    return TransientMediaScope(
        user_id=pending.user_id,
        session_id=pending.session_id,
        call_id=pending.call_id,
    )


__all__ = [
    "ClientToolError",
    "ClientToolExecution",
    "InMemoryClientToolStore",
    "PendingClientTool",
    "client_tool",
    "client_tool_execution",
    "current_transient_media",
]
