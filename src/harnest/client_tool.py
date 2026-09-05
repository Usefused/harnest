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
from typing import Any, Iterator, TypeVar, overload

from .approval import ApprovalRun, bind_tool_arguments
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

    def public(self) -> dict[str, Any]:
        """Return the client-safe fields required to execute this tool call."""

        return {
            "id": self.id,
            "callId": self.call_id,
            "name": self.name,
            "arguments": dict(self.arguments),
            "expiresAt": datetime.fromtimestamp(
                self.expires_at, tz=timezone.utc
            ).isoformat(),
        }


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
    ) -> Any:
        """Suspend an invocation until the client submits a result or times out."""

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
        )
        with self._lock:
            self._items[pending.id] = pending
        _audit(name, "requested", trigger="agent", outcome="suspended")
        run.notifications.put_nowait(("client_tool", pending))
        try:
            return await asyncio.wait_for(pending.future, timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            _audit(name, "expired", trigger="agent", outcome="failed")
            raise ClientToolError(f"client tool {name!r} timed out") from exc
        finally:
            with self._lock:
                self._items.pop(pending.id, None)

    async def submit(
        self, request_id: str, *, user_id: str, output: Any
    ) -> PendingClientTool:
        """Validate and sanitize a client result before its framework resumes."""

        pending = self._reserve_submission(request_id, user_id)
        try:
            prepared = await self._prepared_output(pending, output)
        except (
            ValueError,
            AssetStoreError,
            StoredMediaError,
            TransientMediaError,
        ) as exc:
            self._release_submission(pending)
            raise ClientToolError(str(exc)) from exc
        except BaseException:
            self._release_submission(pending)
            raise
        with self._lock:
            if pending.future.done():
                pending.submitting = False
                committed = False
            else:
                pending.future.set_result(prepared.value)
                committed = True
            pending.submitting = False
        if not committed:
            if prepared.durable is not None:
                await prepared.durable.rollback(
                    scope=AssetScope(pending.user_id, pending.session_id)
                )
            raise ClientToolError("client tool result was already submitted")
        _audit(
            pending.name,
            "result_submitted",
            trigger="user",
            outcome="committed",
        )
        return pending

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
        """Validate and stage typed media after submission is reserved."""

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
    ) -> Any:
        """Request one client-hosted tool call and await its validated result."""

        return await self.store.suspend(
            self.run,
            name=name,
            arguments=arguments,
            output_schema=output_schema,
            timeout_seconds=timeout_seconds,
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
    """Declare an optionally permissioned tool implemented by the client."""

    if not isinstance(timeout_seconds, int) or timeout_seconds < 1:
        raise ValueError("client tool timeout_seconds must be a positive integer")
    if permission is not None:
        from .agent_principal import validate_permission

        validate_permission(permission)
    configured_schema = validate_output_schema(
        output_schema, field_name="client tool output_schema"
    )

    def decorate(fn: F) -> F:
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
        from .approval import wrap_approved_tool

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
