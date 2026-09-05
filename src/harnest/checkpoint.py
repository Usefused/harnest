"""Portable checkpoint ownership and in-progress execution storage."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Literal, Protocol, runtime_checkable

from .logging import get_logger
from .session import InMemorySessionStore

try:
    from langgraph.checkpoint.base import BaseCheckpointSaver as _LangGraphSaverBase
except ImportError:  # ADK-only environments must still import checkpoint authorities.
    class _LangGraphSaverBase:
        def __init__(self, *, serde: Any = None) -> None:
            self.serde = serde

RunStatus = Literal["running", "waiting", "completed", "failed", "cancelled"]
CheckpointOwner = Literal["harnest", "langgraph", "adk"]

_TERMINAL = frozenset({"completed", "failed", "cancelled"})
_AUDIT = get_logger("checkpoint.audit")


class CheckpointError(RuntimeError):
    """Base error for checkpoint persistence and ownership."""


class CheckpointConflictError(CheckpointError):
    """A compare-and-swap or run exclusivity condition failed."""


class A2ATaskConflictError(CheckpointError):
    """An A2A task mutation conflicts with its durable ownership."""


class A2ATaskCursorError(CheckpointError):
    """An A2A list cursor does not identify a task in the selected scope."""


@dataclass(frozen=True, slots=True)
class A2ATaskRecord:
    """Transport-neutral durable snapshot of one protobuf A2A task."""

    application_id: str
    user_id: str
    task_id: str
    context_id: str
    status: int
    payload: bytes
    status_timestamp: str | None = None
    created_at: str = field(default_factory=lambda: _timestamp())
    updated_at: str = field(default_factory=lambda: _timestamp())

    def __post_init__(self) -> None:
        """Reject malformed index projections before storing opaque payloads."""

        _require_fields(
            self.application_id, self.user_id, self.task_id, self.context_id
        )
        if type(self.status) is not int or self.status < 0:
            raise ValueError("A2A task status must be a non-negative integer")
        if not isinstance(self.payload, bytes):
            raise TypeError("A2A task payload must be bytes")
        _parse_optional_timestamp(self.status_timestamp)
        _parse_optional_timestamp(self.created_at)
        _parse_optional_timestamp(self.updated_at)


@dataclass(frozen=True, slots=True)
class A2ATaskPage:
    """One bounded durable task page plus its pre-pagination total."""

    records: tuple[A2ATaskRecord, ...]
    total_size: int

    def __post_init__(self) -> None:
        """Keep page accounting explicit at every storage boundary."""

        if type(self.total_size) is not int or self.total_size < 0:
            raise ValueError("A2A task total_size must be non-negative")


@runtime_checkable
class A2ATaskPersistence(Protocol):
    """Optional indexed A2A persistence implemented by built-in stores."""

    async def put_a2a_task(self, record: A2ATaskRecord) -> A2ATaskRecord:
        """Create or update one owner-scoped A2A task snapshot."""

        ...

    async def get_a2a_task(
        self, *, application_id: str, user_id: str, task_id: str
    ) -> A2ATaskRecord | None:
        """Read one A2A task through its complete ownership key."""

        ...

    async def list_a2a_tasks(
        self,
        *,
        application_id: str,
        user_id: str,
        context_id: str | None = None,
        status: int | None = None,
        status_timestamp_after: str | None = None,
        cursor_task_id: str | None = None,
        limit: int = 51,
    ) -> A2ATaskPage:
        """Return a filtered, cursor-based page of owner-scoped A2A tasks."""

        ...

    async def delete_a2a_task(
        self, *, application_id: str, user_id: str, task_id: str
    ) -> bool:
        """Delete one owner-scoped A2A task and report whether it existed."""

        ...


@dataclass(frozen=True, slots=True)
class PendingAction:
    """Portable description of a durable wait without customer payloads."""

    type: Literal["human_approval", "client_tool", "external_continuation"]
    action_id: str
    capability: str


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One framework execution associated with a committed public session."""

    application_id: str
    user_id: str
    session_id: str
    run_id: str
    framework: Literal["adk", "langgraph"]
    status: RunStatus = "running"
    revision: int = 0
    pending_action: PendingAction | None = None
    created_at: str = field(default_factory=lambda: _timestamp())
    updated_at: str = field(default_factory=lambda: _timestamp())

    @property
    def scope(self) -> RunScope:
        """Return the complete ownership key required for later operations."""

        return RunScope(
            self.application_id, self.user_id, self.session_id, self.run_id
        )


@dataclass(frozen=True, slots=True)
class RunScope:
    """Complete ownership key required at the store boundary for one run."""

    application_id: str
    user_id: str
    session_id: str
    run_id: str

    def __post_init__(self) -> None:
        _require_fields(
            self.application_id, self.user_id, self.session_id, self.run_id
        )


@dataclass(frozen=True, slots=True)
class CheckpointWrite:
    """Opaque framework write attached to a checkpoint task."""

    task_id: str
    channel: str
    type_name: str
    payload: bytes
    task_path: str = ""


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    """Opaque framework checkpoint with portable lineage and CAS revision."""

    run_id: str
    checkpoint_id: str
    namespace: str
    framework: Literal["adk", "langgraph"]
    type_name: str
    payload: bytes
    metadata_type: str
    metadata: bytes
    versions_type: str
    versions: bytes
    parent_checkpoint_id: str | None = None
    revision: int = 0
    created_at: str = field(default_factory=lambda: _timestamp())


@runtime_checkable
class CheckpointStore(Protocol):
    """Storage contract for resumable, in-progress framework execution."""

    async def start(self) -> None:
        """Initialize resources required by the checkpoint store."""

        ...

    async def begin_run(
        self,
        *,
        application_id: str,
        user_id: str,
        session_id: str,
        run_id: str,
        framework: Literal["adk", "langgraph"],
    ) -> RunRecord:
        """Create or recover the active checkpoint run for a session."""

        ...

    async def get_run(self, *, scope: RunScope) -> RunRecord | None:
        """Read a checkpoint run through its complete ownership scope."""

        ...

    async def get_checkpoint(
        self,
        *,
        scope: RunScope,
        checkpoint_id: str | None = None,
        namespace: str = "",
    ) -> CheckpointRecord | None:
        """Read an exact or latest checkpoint within an owned run."""

        ...

    def list_checkpoints(
        self,
        *,
        scope: RunScope,
        namespace: str | None = None,
        before: str | None = None,
        limit: int = 20,
    ) -> AsyncIterator[CheckpointRecord]:
        """Stream a bounded checkpoint history for an owned run."""

        ...

    async def put(
        self,
        checkpoint: CheckpointRecord,
        *,
        scope: RunScope,
        expected_revision: int | None,
    ) -> CheckpointRecord:
        """Persist a checkpoint when its expected revision still matches."""

        ...

    async def put_writes(
        self,
        *,
        scope: RunScope,
        checkpoint_id: str,
        writes: Sequence[CheckpointWrite],
    ) -> None:
        """Persist pending task writes for one checkpoint."""

        ...

    async def get_writes(
        self, *, scope: RunScope, checkpoint_id: str
    ) -> Sequence[CheckpointWrite]:
        """Read pending writes for one owned checkpoint."""

        ...

    async def get_writes_batch(
        self, *, scope: RunScope, checkpoint_ids: Sequence[str]
    ) -> Mapping[str, Sequence[CheckpointWrite]]:
        """Read pending writes for several owned checkpoints in one call."""

        ...

    async def transition(
        self,
        *,
        scope: RunScope,
        expected_status: RunStatus,
        status: RunStatus,
        pending_action: PendingAction | None = None,
    ) -> RunRecord:
        """Apply one compare-and-swap run-state transition."""

        ...

    async def delete_run(self, *, scope: RunScope) -> None:
        """Delete an owned run and all of its checkpoint state."""

        ...

    async def close(self) -> None:
        """Release resources owned by the checkpoint store."""

        ...


class CheckpointAuthority:
    """Typed lifecycle value identifying one checkpoint authority."""

    owner: CheckpointOwner
    framework: str | None
    schema_id: str


class HarnestStore(CheckpointAuthority):
    """Base for framework-neutral stores owned by the Harnest runtime."""

    owner: CheckpointOwner = "harnest"
    framework = None
    schema_id = "harnest-checkpoint/v1"

    def as_langgraph_checkpointer(self) -> Any:
        """Return one stable native saver for advanced LangGraph construction."""

        saver = getattr(self, "_harnest_langgraph_saver", None)
        if saver is None:
            from .checkpoint_langgraph import HarnestCheckpointSaver

            saver = HarnestCheckpointSaver(self)
            setattr(self, "_harnest_langgraph_saver", saver)
        return saver


@dataclass(slots=True)
class _MemoryState:
    runs: dict[str, RunRecord] = field(default_factory=dict)
    active: dict[tuple[str, str, str], str] = field(default_factory=dict)
    checkpoints: dict[tuple[str, str, str], CheckpointRecord] = field(
        default_factory=dict
    )
    writes: dict[tuple[str, str], list[CheckpointWrite]] = field(
        default_factory=dict
    )
    continuations: dict[str, Any] = field(default_factory=dict)
    continuation_external_ids: dict[str, str] = field(default_factory=dict)
    continuation_external_keys: dict[tuple[str, str, str], str] = field(
        default_factory=dict
    )
    a2a_tasks: dict[tuple[str, str, str], A2ATaskRecord] = field(
        default_factory=dict
    )


class MemoryStore(HarnestStore, InMemorySessionStore):
    """Process-local session, checkpoint, and continuation store with atomic CAS."""

    def __init__(self) -> None:
        """Keep session and checkpoint state behind one process-local lock."""

        InMemorySessionStore.__init__(self)
        self._state = _MemoryState()
        self._lock = asyncio.Lock()
        self._closed = False

    async def start(self) -> None:
        """Reject reopening because closed development state is intentionally lost."""

        if self._closed:
            raise RuntimeError("checkpoint store is closed")
        await InMemorySessionStore.start(self)

    async def put_a2a_task(self, record: A2ATaskRecord) -> A2ATaskRecord:
        """Upsert one owner-scoped snapshot without allowing context reassignment."""

        key = _a2a_task_key(record)
        try:
            async with self._lock:
                current = self._state.a2a_tasks.get(key)
                if current is not None and current.context_id != record.context_id:
                    raise A2ATaskConflictError(
                        "A2A task context cannot change after creation"
                    )
                stored = replace(
                    record,
                    created_at=(
                        record.created_at if current is None else current.created_at
                    ),
                    updated_at=_timestamp(),
                )
                self._state.a2a_tasks[key] = stored
        except Exception:
            _audit_a2a_task("saved", "failed", "memory")
            raise
        _audit_a2a_task("saved", "committed", "memory")
        return stored

    async def get_a2a_task(
        self, *, application_id: str, user_id: str, task_id: str
    ) -> A2ATaskRecord | None:
        """Read one snapshot only through its complete ownership key."""

        _require_fields(application_id, user_id, task_id)
        async with self._lock:
            return self._state.a2a_tasks.get(
                (application_id, user_id, task_id)
            )

    async def list_a2a_tasks(
        self,
        *,
        application_id: str,
        user_id: str,
        context_id: str | None = None,
        status: int | None = None,
        status_timestamp_after: str | None = None,
        cursor_task_id: str | None = None,
        limit: int = 51,
    ) -> A2ATaskPage:
        """Return a stable inclusive-cursor page for the process-local backend."""

        _require_a2a_list_options(
            application_id,
            user_id,
            context_id,
            status,
            status_timestamp_after,
            cursor_task_id,
            limit,
        )
        async with self._lock:
            records = tuple(self._state.a2a_tasks.values())
        selected = _select_a2a_tasks(
            records,
            application_id=application_id,
            user_id=user_id,
            context_id=context_id,
            status=status,
            status_timestamp_after=status_timestamp_after,
        )
        start = _a2a_cursor_offset(selected, cursor_task_id)
        return A2ATaskPage(selected[start : start + limit], len(selected))

    async def delete_a2a_task(
        self, *, application_id: str, user_id: str, task_id: str
    ) -> bool:
        """Delete exactly one owner-scoped A2A snapshot."""

        _require_fields(application_id, user_id, task_id)
        try:
            async with self._lock:
                removed = self._state.a2a_tasks.pop(
                    (application_id, user_id, task_id), None
                )
        except Exception:
            _audit_a2a_task("deleted", "failed", "memory")
            raise
        if removed is not None:
            _audit_a2a_task("deleted", "committed", "memory")
        return removed is not None

    async def begin_run(
        self,
        *,
        application_id: str,
        user_id: str,
        session_id: str,
        run_id: str,
        framework: Literal["adk", "langgraph"],
    ) -> RunRecord:
        """Claim the session for one idempotent invocation run."""

        _require_fields(application_id, user_id, session_id, run_id)
        key = (application_id, user_id, session_id)
        async with self._lock:
            existing = self._state.runs.get(run_id)
            if existing is not None:
                return _validate_same_run(
                    existing,
                    application_id,
                    user_id,
                    session_id,
                    framework,
                )
            active = self._state.active.get(key)
            if active is not None:
                raise CheckpointConflictError(
                    f"session already has active checkpoint run {active!r}"
                )
            record = RunRecord(*key, run_id, framework)
            self._state.runs[run_id] = record
            self._state.active[key] = run_id
        _audit(record, "run_started", "committed")
        return record

    async def get_run(self, *, scope: RunScope) -> RunRecord | None:
        """Read one process-local run through its complete ownership scope."""

        async with self._lock:
            return _owned_run(self._state.runs.get(scope.run_id), scope)

    async def get_checkpoint(
        self,
        *,
        scope: RunScope,
        checkpoint_id: str | None = None,
        namespace: str = "",
    ) -> CheckpointRecord | None:
        """Load an exact checkpoint or the newest checkpoint in a namespace."""

        async with self._lock:
            if _owned_run(self._state.runs.get(scope.run_id), scope) is None:
                return None
            if checkpoint_id is not None:
                return self._state.checkpoints.get(
                    (scope.run_id, namespace, checkpoint_id)
                )
            matches = (
                value
                for key, value in self._state.checkpoints.items()
                if key[0] == scope.run_id and key[1] == namespace
            )
            return max(matches, key=lambda item: item.revision, default=None)

    async def list_checkpoints(
        self,
        *,
        scope: RunScope,
        namespace: str | None = None,
        before: str | None = None,
        limit: int = 20,
    ) -> AsyncIterator[CheckpointRecord]:
        """Yield a bounded newest-first checkpoint history."""

        if limit < 1:
            raise ValueError("checkpoint list limit must be positive")
        async with self._lock:
            if _owned_run(self._state.runs.get(scope.run_id), scope) is None:
                return
            values = tuple(
                value
                for key, value in self._state.checkpoints.items()
                if key[0] == scope.run_id
                and (namespace is None or key[1] == namespace)
                and (before is None or value.checkpoint_id < before)
            )
        for value in sorted(
            values, key=lambda item: item.revision, reverse=True
        )[:limit]:
            yield value

    async def put(
        self,
        checkpoint: CheckpointRecord,
        *,
        scope: RunScope,
        expected_revision: int | None,
    ) -> CheckpointRecord:
        """Persist a checkpoint only when its expected revision still matches."""

        _require_checkpoint_scope(checkpoint, scope)
        key = (checkpoint.run_id, checkpoint.namespace, checkpoint.checkpoint_id)
        async with self._lock:
            record = _require_owned_run(
                self._state.runs.get(checkpoint.run_id), scope
            )
            _require_running(record)
            current = self._state.checkpoints.get(key)
            current_revision = -1 if current is None else current.revision
            if current_revision != (
                -1 if expected_revision is None else expected_revision
            ):
                raise CheckpointConflictError("checkpoint revision changed")
            revision = max(
                (
                    item.revision
                    for item in self._state.checkpoints.values()
                    if item.run_id == checkpoint.run_id
                    and item.namespace == checkpoint.namespace
                ),
                default=-1,
            )
            stored = replace(checkpoint, revision=revision + 1)
            self._state.checkpoints[key] = stored
        _audit(self._state.runs[checkpoint.run_id], "checkpoint_saved", "committed")
        return stored

    async def put_writes(
        self,
        *,
        scope: RunScope,
        checkpoint_id: str,
        writes: Sequence[CheckpointWrite],
    ) -> None:
        """Add pending writes idempotently by task and channel."""

        async with self._lock:
            record = _require_owned_run(
                self._state.runs.get(scope.run_id), scope
            )
            _require_running(record)
            target = self._state.writes.setdefault(
                (scope.run_id, checkpoint_id), []
            )
            existing = {(item.task_id, item.channel) for item in target}
            target.extend(
                item
                for item in writes
                if (item.task_id, item.channel) not in existing
            )
        _audit(record, "writes_saved", "committed")

    async def get_writes(
        self, *, scope: RunScope, checkpoint_id: str
    ) -> Sequence[CheckpointWrite]:
        """Read process-local pending writes for one owned checkpoint."""

        async with self._lock:
            if _owned_run(self._state.runs.get(scope.run_id), scope) is None:
                return ()
            return tuple(
                self._state.writes.get((scope.run_id, checkpoint_id), ())
            )

    async def get_writes_batch(
        self, *, scope: RunScope, checkpoint_ids: Sequence[str]
    ) -> Mapping[str, Sequence[CheckpointWrite]]:
        """Load writes for several checkpoints without per-checkpoint calls."""

        async with self._lock:
            if _owned_run(self._state.runs.get(scope.run_id), scope) is None:
                return {}
            return {
                checkpoint_id: tuple(
                    self._state.writes.get((scope.run_id, checkpoint_id), ())
                )
                for checkpoint_id in checkpoint_ids
            }

    async def transition(
        self,
        *,
        scope: RunScope,
        expected_status: RunStatus,
        status: RunStatus,
        pending_action: PendingAction | None = None,
    ) -> RunRecord:
        """Apply one valid compare-and-swap run-state transition."""

        _validate_transition(expected_status, status, pending_action)
        async with self._lock:
            current = _require_owned_run(
                self._state.runs.get(scope.run_id), scope
            )
            if current.status != expected_status:
                raise CheckpointConflictError("checkpoint run status changed")
            updated = replace(
                current,
                status=status,
                pending_action=pending_action,
                revision=current.revision + 1,
                updated_at=_timestamp(),
            )
            self._state.runs[scope.run_id] = updated
            if status in _TERMINAL:
                self._state.active.pop(_run_key(current), None)
        _audit(updated, f"run_{status}", "committed")
        return updated

    async def suspend_continuation(
        self, *, record: Any, external_id: str
    ) -> Any:
        """Atomically register provider work and move its owned run to waiting."""

        from .continuation import (
            ContinuationConflictError,
            audit_continuation,
            external_id_key,
        )

        key = (
            record.application_id,
            record.provider,
            external_id_key(record.provider, external_id),
        )
        try:
            async with self._lock:
                run = _owned_run(
                    self._state.runs.get(record.run_id), record.scope
                )
                if run is None or run.status != "running":
                    raise ContinuationConflictError("continuation run is not running")
                if record.continuation_id in self._state.continuations or key in self._state.continuation_external_keys:
                    raise ContinuationConflictError("continuation already exists")
                self._state.continuations[record.continuation_id] = record
                self._state.continuation_external_ids[record.continuation_id] = external_id
                self._state.continuation_external_keys[key] = record.continuation_id
                self._state.runs[run.run_id] = replace(
                    run,
                    status="waiting",
                    pending_action=record.pending_action,
                    revision=run.revision + 1,
                    updated_at=_timestamp(),
                )
        except Exception:
            audit_continuation("suspended", "failed", "memory")
            raise
        audit_continuation("suspended", "committed", "memory")
        return record

    async def get_continuation(
        self, *, scope: RunScope, continuation_id: str
    ) -> Any | None:
        """Return one record only when every run ownership field matches."""

        async with self._lock:
            record = self._state.continuations.get(continuation_id)
            return record if record is not None and record.scope == scope else None

    async def get_provider_continuation(
        self, *, scope: RunScope, provider: str, continuation_id: str
    ) -> Any | None:
        """Return an exact continuation together with its provider external ID."""

        from .continuation import ProviderPendingContinuation

        _require_fields(provider, continuation_id)
        async with self._lock:
            record = self._state.continuations.get(continuation_id)
            external_id = self._state.continuation_external_ids.get(
                continuation_id
            )
            if (
                record is None
                or external_id is None
                or record.scope != scope
                or record.provider != provider
            ):
                return None
            return ProviderPendingContinuation(record, external_id)

    async def get_continuation_by_external_id(
        self, *, application_id: str, provider: str, external_id: str
    ) -> Any | None:
        """Resolve one provider identity through the same indexed ownership key."""

        from .continuation import ProviderPendingContinuation, external_id_key

        _require_fields(application_id, provider, external_id)
        key = (application_id, provider, external_id_key(provider, external_id))
        async with self._lock:
            continuation_id = self._state.continuation_external_keys.get(key)
            record = self._state.continuations.get(continuation_id or "")
            if record is None:
                return None
            stored_external_id = self._state.continuation_external_ids.get(
                record.continuation_id
            )
            if stored_external_id is None:
                return None
            return ProviderPendingContinuation(record, stored_external_id)

    async def list_pending_continuations(
        self,
        *,
        application_id: str,
        provider: str,
        after: str | None = None,
        limit: int = 100,
    ) -> Sequence[Any]:
        """Return a bounded provider page for local reconciliation."""

        from .continuation import ProviderPendingContinuation, _require_page

        _require_fields(application_id, provider)
        _require_page(after, limit)
        async with self._lock:
            records = sorted(
                (
                    value
                    for value in self._state.continuations.values()
                    if value.application_id == application_id
                    and value.provider == provider
                    and value.status == "pending"
                    and (after is None or value.continuation_id > after)
                ),
                key=lambda value: value.continuation_id,
            )[:limit]
            return tuple(
                ProviderPendingContinuation(
                    value,
                    self._state.continuation_external_ids[value.continuation_id],
                )
                for value in records
            )

    async def resolve_continuation(
        self,
        *,
        scope: RunScope,
        provider: str,
        external_id: str,
        schema_id: str,
        result: Any = None,
        failure: Any = None,
    ) -> Any:
        """Commit a provider result or failure with one status compare-and-swap."""

        from .continuation import (
            ContinuationConflictError,
            _validated_resolution,
            audit_continuation,
            external_id_key,
        )

        result, failure = _validated_resolution(result, failure)
        key = (scope.application_id, provider, external_id_key(provider, external_id))
        try:
            async with self._lock:
                continuation_id = self._state.continuation_external_keys.get(key)
                record = self._state.continuations.get(continuation_id or "")
                if record is None or record.scope != scope:
                    raise ContinuationConflictError("continuation state changed")
                if record.status != "pending" or record.schema_id != schema_id:
                    raise ContinuationConflictError("continuation state changed")
                status = "failed" if failure is not None else "completed"
                updated = replace(
                    record,
                    status=status,
                    result=result,
                    failure=failure,
                    revision=record.revision + 1,
                    updated_at=_timestamp(),
                )
                self._state.continuations[record.continuation_id] = updated
        except Exception:
            audit_continuation("resolved", "failed", "memory")
            raise
        audit_continuation("resolved", "committed", "memory")
        return updated

    async def cancel_continuation(
        self,
        *,
        scope: RunScope,
        provider: str,
        continuation_id: str,
        expected_revision: int,
        failure: Any,
    ) -> Any:
        """Fail one pending continuation and cancel its exact waiting run."""

        from .continuation import (
            ContinuationConflictError,
            ContinuationFailure,
            audit_continuation,
        )

        if not isinstance(failure, ContinuationFailure):
            audit_continuation("cancelled", "failed", "memory")
            raise TypeError("continuation cancellation failure is invalid")

        try:
            async with self._lock:
                record = self._state.continuations.get(continuation_id)
                run = _owned_run(self._state.runs.get(scope.run_id), scope)
                if not _valid_continuation_cancel(
                    record,
                    run,
                    provider,
                    continuation_id,
                    expected_revision,
                ):
                    raise ContinuationConflictError(
                        "continuation cancellation changed"
                    )
                updated = replace(
                    record,
                    status="failed",
                    failure=failure,
                    revision=record.revision + 1,
                    updated_at=_timestamp(),
                )
                self._state.continuations[continuation_id] = updated
                self._state.runs[run.run_id] = replace(
                    run,
                    status="cancelled",
                    pending_action=None,
                    revision=run.revision + 1,
                    updated_at=_timestamp(),
                )
                self._state.active.pop(_run_key(run), None)
        except Exception:
            audit_continuation("cancelled", "failed", "memory")
            raise
        audit_continuation("cancelled", "committed", "memory")
        return updated

    async def claim_continuation(
        self,
        *,
        scope: RunScope,
        provider: str,
        continuation_id: str,
        expected_revision: int,
    ) -> Any:
        """Claim an outcome and return the waiting run to running atomically."""

        from .continuation import ContinuationConflictError, audit_continuation

        try:
            async with self._lock:
                record = self._state.continuations.get(continuation_id)
                run = _owned_run(self._state.runs.get(scope.run_id), scope)
                if (
                    record is None
                    or record.scope != scope
                    or record.provider != provider
                    or run is None
                ):
                    raise ContinuationConflictError("continuation claim changed")
                if not _valid_continuation_claim(
                    record, run, continuation_id, expected_revision
                ):
                    raise ContinuationConflictError("continuation claim changed")
                updated = replace(
                    record,
                    status="claimed",
                    revision=record.revision + 1,
                    updated_at=_timestamp(),
                )
                self._state.continuations[continuation_id] = updated
                self._state.runs[run.run_id] = replace(
                    run,
                    status="running",
                    pending_action=None,
                    revision=run.revision + 1,
                    updated_at=_timestamp(),
                )
        except Exception:
            audit_continuation("claimed", "failed", "memory")
            raise
        audit_continuation("claimed", "committed", "memory")
        return updated

    async def arm_continuation(
        self,
        *,
        scope: RunScope,
        provider: str,
        continuation_id: str,
        expected_revision: int,
    ) -> Any:
        """Arm a provider wait after its native suspension is checkpointed."""

        from .continuation import ContinuationConflictError, audit_continuation

        try:
            async with self._lock:
                record = self._state.continuations.get(continuation_id)
                valid = (
                    record is not None
                    and record.scope == scope
                    and record.provider == provider
                    and record.revision == expected_revision
                    and not record.ready
                    and record.status in {"pending", "completed", "failed"}
                )
                if not valid:
                    raise ContinuationConflictError("continuation arm changed")
                updated = replace(
                    record,
                    ready=True,
                    revision=record.revision + 1,
                    updated_at=_timestamp(),
                )
                self._state.continuations[continuation_id] = updated
        except Exception:
            audit_continuation("armed", "failed", "memory")
            raise
        audit_continuation("armed", "committed", "memory")
        return updated

    async def delete_run(self, *, scope: RunScope) -> None:
        """Delete a run and every private checkpoint owned by it."""

        async with self._lock:
            record = _owned_run(self._state.runs.get(scope.run_id), scope)
            if record is None:
                return
            self._state.runs.pop(scope.run_id)
            self._state.active.pop(_run_key(record), None)
            for key in tuple(self._state.checkpoints):
                if key[0] == scope.run_id:
                    self._state.checkpoints.pop(key)
            for key in tuple(self._state.writes):
                if key[0] == scope.run_id:
                    self._state.writes.pop(key)
            continuation_ids = tuple(
                value.continuation_id
                for value in self._state.continuations.values()
                if value.run_id == scope.run_id
            )
            for continuation_id in continuation_ids:
                self._remove_continuation(continuation_id)
        _audit(record, "run_deleted", "committed")

    def _remove_continuation(self, continuation_id: str) -> None:
        """Remove private mappings together so deleted runs cannot be reconciled."""

        from .continuation import external_id_key

        record = self._state.continuations.pop(continuation_id, None)
        external_id = self._state.continuation_external_ids.pop(
            continuation_id, None
        )
        if record is None or external_id is None:
            return
        key = (
            record.application_id,
            record.provider,
            external_id_key(record.provider, external_id),
        )
        self._state.continuation_external_keys.pop(key, None)

    async def close(self) -> None:
        """Close both session and checkpoint sides of the shared store."""

        await InMemorySessionStore.close(self)
        self._closed = True


class LangGraphStore(CheckpointAuthority, _LangGraphSaverBase):
    """Explicit lifecycle ownership wrapper for a native LangGraph saver."""

    owner: CheckpointOwner = "langgraph"
    framework = "langgraph"
    schema_id = "langgraph-native"

    def __init__(self, checkpointer: Any) -> None:
        """Wrap the exact saver used by an advanced compiled graph."""

        if checkpointer is None:
            raise ValueError("LangGraphStore requires a native checkpointer")
        self.checkpointer = checkpointer

        # LangGraph validates the lifecycle value with isinstance before it can
        # use delegation, so the wrapper implements its native saver base class.
        _LangGraphSaverBase.__init__(
            self, serde=getattr(checkpointer, "serde", None)
        )

    @property
    def config_specs(self) -> list[Any]:
        """Expose the wrapped saver configuration fields to LangGraph."""

        return self.checkpointer.config_specs

    def get_tuple(self, config: Any) -> Any:
        """Delegate synchronous checkpoint lookup to the wrapped saver."""

        return self.checkpointer.get_tuple(config)

    def list(self, config: Any, **options: Any) -> Any:
        """Delegate synchronous checkpoint listing to the wrapped saver."""

        return self.checkpointer.list(config, **options)

    def put(
        self,
        config: Any,
        checkpoint: Any,
        metadata: Any,
        new_versions: Any,
    ) -> Any:
        """Delegate synchronous checkpoint persistence to the wrapped saver."""

        return self.checkpointer.put(
            config, checkpoint, metadata, new_versions
        )

    def put_writes(
        self,
        config: Any,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Delegate synchronous pending-write persistence to the wrapped saver."""

        self.checkpointer.put_writes(config, writes, task_id, task_path)

    def delete_thread(self, thread_id: str) -> None:
        """Delete all checkpoints for a LangGraph thread."""

        self.checkpointer.delete_thread(thread_id)

    async def aget_tuple(self, config: Any) -> Any:
        """Delegate asynchronous checkpoint lookup to the wrapped saver."""

        return await self.checkpointer.aget_tuple(config)

    async def alist(self, config: Any, **options: Any) -> AsyncIterator[Any]:
        """Stream checkpoints from the wrapped asynchronous saver."""

        async for value in self.checkpointer.alist(config, **options):
            yield value

    async def aput(
        self,
        config: Any,
        checkpoint: Any,
        metadata: Any,
        new_versions: Any,
    ) -> Any:
        """Delegate asynchronous checkpoint persistence to the wrapped saver."""

        return await self.checkpointer.aput(
            config, checkpoint, metadata, new_versions
        )

    async def aput_writes(
        self,
        config: Any,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Delegate asynchronous pending-write persistence to the wrapped saver."""

        await self.checkpointer.aput_writes(
            config, writes, task_id, task_path
        )

    async def adelete_thread(self, thread_id: str) -> None:
        """Asynchronously delete all checkpoints for a LangGraph thread."""

        await self.checkpointer.adelete_thread(thread_id)

    def get_next_version(self, current: Any, channel: Any) -> Any:
        """Ask the wrapped saver to derive the next channel version."""

        return self.checkpointer.get_next_version(current, channel)

    def __getattr__(self, name: str) -> Any:
        # Delegation makes the lifecycle-owned wrapper usable directly in
        # builder.compile(checkpointer=...) without hiding its ownership.
        return getattr(self.checkpointer, name)

    async def start(self) -> None:
        """Initialize the wrapped saver when it owns a start hook."""

        await _optional_async_call(self.checkpointer, "start")

    async def close(self) -> None:
        """Release resources owned by the wrapped saver."""

        await _optional_async_call(self.checkpointer, "close")


class ADKStore(CheckpointAuthority):
    """Explicit lifecycle ownership wrapper for native ADK persistence."""

    owner: CheckpointOwner = "adk"
    framework = "adk"
    schema_id = "adk-native"

    def __init__(self, session_service: Any) -> None:
        """Declare one native ADK service as both session and checkpoint authority."""

        if session_service is None:
            raise ValueError("ADKStore requires a native session service")
        self.session_service = session_service

    async def start(self) -> None:
        """Initialize the wrapped ADK session service when supported."""

        await _optional_async_call(self.session_service, "start")

    async def close(self) -> None:
        """Release resources owned by the wrapped ADK session service."""

        await _optional_async_call(self.session_service, "close")


def checkpoint_metadata(authority: CheckpointAuthority) -> dict[str, str]:
    """Return immutable, non-secret ownership metadata for an artifact."""

    return {
        "owner": authority.owner,
        "framework": authority.framework or "portable",
        "schema": authority.schema_id,
    }


async def _optional_async_call(value: Any, name: str) -> None:
    """Invoke an optional lifecycle method without requiring an async-only API."""

    function = getattr(value, name, None)
    if not callable(function):
        return
    result = function()
    if hasattr(result, "__await__"):
        await result


def _validate_same_run(
    record: RunRecord,
    application_id: str,
    user_id: str,
    session_id: str,
    framework: Literal["adk", "langgraph"],
) -> RunRecord:
    """Keep idempotent run creation scoped to the original execution."""

    expected = (application_id, user_id, session_id, framework)
    actual = (*_run_key(record), record.framework)
    if actual != expected:
        raise CheckpointConflictError("run_id belongs to another execution")
    return record


def _require_running(record: RunRecord | None) -> None:
    """Block checkpoint writes after an invocation reaches terminal state."""

    if record is None:
        raise KeyError("checkpoint run not found")
    if record.status not in {"running", "waiting"}:
        raise CheckpointConflictError("checkpoint run is already terminal")


def _owned_run(record: RunRecord | None, scope: RunScope) -> RunRecord | None:
    """Return a run only when every ownership component matches."""

    if record is None or record.scope != scope:
        return None
    return record


def _valid_continuation_claim(
    record: Any, run: RunRecord, continuation_id: str, expected_revision: int
) -> bool:
    """Require the outcome and waiting run to name the same resumable boundary."""

    pending_id = getattr(run.pending_action, "action_id", None)
    return (
        record.status in {"completed", "failed"}
        and record.ready
        and record.revision == expected_revision
        and run.status == "waiting"
        and pending_id == continuation_id
    )


def _valid_continuation_cancel(
    record: Any,
    run: RunRecord | None,
    provider: str,
    continuation_id: str,
    expected_revision: int,
) -> bool:
    """Require a pending provider wait and its run to name one cancellation."""

    if record is None or run is None:
        return False
    pending_id = getattr(run.pending_action, "action_id", None)
    return (
        record.scope == run.scope
        and record.provider == provider
        and record.status == "pending"
        and record.revision == expected_revision
        and run.status == "waiting"
        and pending_id == continuation_id
    )


def _require_owned_run(record: RunRecord | None, scope: RunScope) -> RunRecord:
    """Reject missing and foreign runs with the same non-disclosing error."""

    owned = _owned_run(record, scope)
    if owned is None:
        raise KeyError("checkpoint run not found")
    return owned


def _require_checkpoint_scope(
    checkpoint: CheckpointRecord, scope: RunScope
) -> None:
    if checkpoint.run_id != scope.run_id:
        raise ValueError("checkpoint run_id does not match ownership scope")


def _validate_transition(
    current: RunStatus, target: RunStatus, pending: PendingAction | None
) -> None:
    """Enforce the portable run-state machine before any backend mutation."""

    allowed = {
        "running": {"waiting", "completed", "failed", "cancelled"},
        "waiting": {"running", "failed", "cancelled"},
    }
    if target not in allowed.get(current, set()):
        raise CheckpointConflictError(f"invalid run transition {current} -> {target}")
    if (target == "waiting") != (pending is not None):
        raise ValueError("pending_action is required only for waiting runs")


def _run_key(record: RunRecord) -> tuple[str, str, str]:
    """Use public execution identity as the single-active-run boundary."""

    return record.application_id, record.user_id, record.session_id


def _require_fields(*values: str) -> None:
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("checkpoint run identifiers must be non-empty strings")


def _require_a2a_list_options(
    application_id: str,
    user_id: str,
    context_id: str | None,
    status: int | None,
    status_timestamp_after: str | None,
    cursor_task_id: str | None,
    limit: int,
) -> None:
    """Validate indexed list options consistently across every backend."""

    _require_fields(application_id, user_id)
    for value in (context_id, cursor_task_id):
        if value is not None:
            _require_fields(value)
    if status is not None and (type(status) is not int or status < 0):
        raise ValueError("A2A task status filter must be non-negative")
    _parse_optional_timestamp(status_timestamp_after)
    if type(limit) is not int or limit < 1:
        raise ValueError("A2A task list limit must be positive")


def _select_a2a_tasks(
    records: Sequence[A2ATaskRecord],
    *,
    application_id: str,
    user_id: str,
    context_id: str | None,
    status: int | None,
    status_timestamp_after: str | None,
) -> tuple[A2ATaskRecord, ...]:
    """Apply the reference filtering and stable A2A ordering in memory."""

    after = _parse_optional_timestamp(status_timestamp_after)
    selected = (
        record
        for record in records
        if _matches_a2a_task(
            record,
            application_id=application_id,
            user_id=user_id,
            context_id=context_id,
            status=status,
            after=after,
        )
    )
    return tuple(sorted(selected, key=_a2a_sort_key, reverse=True))


def _matches_a2a_task(
    record: A2ATaskRecord,
    *,
    application_id: str,
    user_id: str,
    context_id: str | None,
    status: int | None,
    after: datetime | None,
) -> bool:
    """Keep optional task predicates separate from ordering and pagination."""

    if record.application_id != application_id or record.user_id != user_id:
        return False
    if context_id is not None and record.context_id != context_id:
        return False
    if status is not None and record.status != status:
        return False
    timestamp = _parse_optional_timestamp(record.status_timestamp)
    return after is None or (timestamp is not None and timestamp >= after)


def _a2a_sort_key(record: A2ATaskRecord) -> tuple[bool, datetime, str]:
    """Sort missing status timestamps last and use task ID as the stable tie."""

    timestamp = _parse_optional_timestamp(record.status_timestamp)
    return (
        timestamp is not None,
        timestamp or datetime.min.replace(tzinfo=timezone.utc),
        record.task_id,
    )


def _a2a_cursor_offset(
    records: Sequence[A2ATaskRecord], cursor_task_id: str | None
) -> int:
    """Resolve the SDK's inclusive lookahead cursor within the selected set."""

    if cursor_task_id is None:
        return 0
    for index, record in enumerate(records):
        if record.task_id == cursor_task_id:
            return index
    raise A2ATaskCursorError("A2A task cursor is not valid for this list")


def _a2a_task_key(record: A2ATaskRecord) -> tuple[str, str, str]:
    """Return the complete task identity, including its compiled application."""

    return record.application_id, record.user_id, record.task_id


def _parse_optional_timestamp(value: str | None) -> datetime | None:
    """Parse one UTC timestamp without accepting ambiguous naive values."""

    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A2A task timestamp must be a non-empty ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("A2A task timestamp must be valid ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("A2A task timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _audit_a2a_task(operation: str, outcome: str, backend: str) -> None:
    """Audit durable task mutations without logging owner IDs or protobuf data."""

    _AUDIT.info(
        f"a2a.task_{operation}",
        operation=f"a2a.task_{operation}",
        trigger="agent",
        outcome=outcome,
        backend=backend,
    )


def _audit(record: RunRecord, operation: str, outcome: str) -> None:
    """Emit mutation identity while excluding checkpoint and customer payloads."""

    # Framework payloads can contain prompts, tool arguments, and credentials.
    # The audit boundary records only stable execution identity and state.
    _AUDIT.info(
        f"checkpoint.{operation}",
        operation=operation,
        trigger="agent",
        outcome=outcome,
        framework=record.framework,
        application_id=record.application_id,
    )


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "A2ATaskConflictError",
    "A2ATaskCursorError",
    "A2ATaskPage",
    "A2ATaskPersistence",
    "A2ATaskRecord",
    "ADKStore",
    "CheckpointConflictError",
    "CheckpointError",
    "CheckpointAuthority",
    "CheckpointRecord",
    "CheckpointStore",
    "CheckpointWrite",
    "HarnestStore",
    "LangGraphStore",
    "MemoryStore",
    "PendingAction",
    "RunRecord",
    "RunScope",
    "RunStatus",
    "checkpoint_metadata",
]
