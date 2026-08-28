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


@dataclass(frozen=True, slots=True)
class PendingAction:
    """Portable description of a durable wait without customer payloads."""

    type: Literal["human_approval", "client_tool"]
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

    async def start(self) -> None: ...

    async def begin_run(
        self,
        *,
        application_id: str,
        user_id: str,
        session_id: str,
        run_id: str,
        framework: Literal["adk", "langgraph"],
    ) -> RunRecord: ...

    async def get_run(self, *, run_id: str) -> RunRecord | None: ...

    async def get_checkpoint(
        self,
        *,
        run_id: str,
        checkpoint_id: str | None = None,
        namespace: str = "",
    ) -> CheckpointRecord | None: ...

    def list_checkpoints(
        self,
        *,
        run_id: str,
        namespace: str | None = None,
        before: str | None = None,
        limit: int = 20,
    ) -> AsyncIterator[CheckpointRecord]: ...

    async def put(
        self, checkpoint: CheckpointRecord, *, expected_revision: int | None
    ) -> CheckpointRecord: ...

    async def put_writes(
        self,
        *,
        run_id: str,
        checkpoint_id: str,
        writes: Sequence[CheckpointWrite],
    ) -> None: ...

    async def get_writes(
        self, *, run_id: str, checkpoint_id: str
    ) -> Sequence[CheckpointWrite]: ...

    async def get_writes_batch(
        self, *, run_id: str, checkpoint_ids: Sequence[str]
    ) -> Mapping[str, Sequence[CheckpointWrite]]: ...

    async def transition(
        self,
        *,
        run_id: str,
        expected_status: RunStatus,
        status: RunStatus,
        pending_action: PendingAction | None = None,
    ) -> RunRecord: ...

    async def delete_run(self, *, run_id: str) -> None: ...

    async def close(self) -> None: ...


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


class MemoryStore(HarnestStore, InMemorySessionStore):
    """Process-local combined session/checkpoint store with atomic CAS."""

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

    async def get_run(self, *, run_id: str) -> RunRecord | None:
        async with self._lock:
            return self._state.runs.get(run_id)

    async def get_checkpoint(
        self,
        *,
        run_id: str,
        checkpoint_id: str | None = None,
        namespace: str = "",
    ) -> CheckpointRecord | None:
        """Load an exact checkpoint or the newest checkpoint in a namespace."""

        async with self._lock:
            if checkpoint_id is not None:
                return self._state.checkpoints.get(
                    (run_id, namespace, checkpoint_id)
                )
            matches = (
                value
                for key, value in self._state.checkpoints.items()
                if key[0] == run_id and key[1] == namespace
            )
            return max(matches, key=lambda item: item.revision, default=None)

    async def list_checkpoints(
        self,
        *,
        run_id: str,
        namespace: str | None = None,
        before: str | None = None,
        limit: int = 20,
    ) -> AsyncIterator[CheckpointRecord]:
        """Yield a bounded newest-first checkpoint history."""

        if limit < 1:
            raise ValueError("checkpoint list limit must be positive")
        async with self._lock:
            values = tuple(
                value
                for key, value in self._state.checkpoints.items()
                if key[0] == run_id
                and (namespace is None or key[1] == namespace)
                and (before is None or value.checkpoint_id < before)
            )
        for value in sorted(
            values, key=lambda item: item.revision, reverse=True
        )[:limit]:
            yield value

    async def put(
        self, checkpoint: CheckpointRecord, *, expected_revision: int | None
    ) -> CheckpointRecord:
        """Persist a checkpoint only when its expected revision still matches."""

        key = (checkpoint.run_id, checkpoint.namespace, checkpoint.checkpoint_id)
        async with self._lock:
            _require_running(self._state.runs.get(checkpoint.run_id))
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
        run_id: str,
        checkpoint_id: str,
        writes: Sequence[CheckpointWrite],
    ) -> None:
        """Add pending writes idempotently by task and channel."""

        async with self._lock:
            _require_running(self._state.runs.get(run_id))
            target = self._state.writes.setdefault((run_id, checkpoint_id), [])
            existing = {(item.task_id, item.channel) for item in target}
            target.extend(
                item
                for item in writes
                if (item.task_id, item.channel) not in existing
            )
        _audit(self._state.runs[run_id], "writes_saved", "committed")

    async def get_writes(
        self, *, run_id: str, checkpoint_id: str
    ) -> Sequence[CheckpointWrite]:
        async with self._lock:
            return tuple(self._state.writes.get((run_id, checkpoint_id), ()))

    async def get_writes_batch(
        self, *, run_id: str, checkpoint_ids: Sequence[str]
    ) -> Mapping[str, Sequence[CheckpointWrite]]:
        """Load writes for several checkpoints without per-checkpoint calls."""

        async with self._lock:
            return {
                checkpoint_id: tuple(
                    self._state.writes.get((run_id, checkpoint_id), ())
                )
                for checkpoint_id in checkpoint_ids
            }

    async def transition(
        self,
        *,
        run_id: str,
        expected_status: RunStatus,
        status: RunStatus,
        pending_action: PendingAction | None = None,
    ) -> RunRecord:
        """Apply one valid compare-and-swap run-state transition."""

        _validate_transition(expected_status, status, pending_action)
        async with self._lock:
            current = self._state.runs.get(run_id)
            if current is None:
                raise KeyError("checkpoint run not found")
            if current.status != expected_status:
                raise CheckpointConflictError("checkpoint run status changed")
            updated = replace(
                current,
                status=status,
                pending_action=pending_action,
                revision=current.revision + 1,
                updated_at=_timestamp(),
            )
            self._state.runs[run_id] = updated
            if status in _TERMINAL:
                self._state.active.pop(_run_key(current), None)
        _audit(updated, f"run_{status}", "committed")
        return updated

    async def delete_run(self, *, run_id: str) -> None:
        """Delete a run and every private checkpoint owned by it."""

        async with self._lock:
            record = self._state.runs.pop(run_id, None)
            if record is None:
                return
            self._state.active.pop(_run_key(record), None)
            for key in tuple(self._state.checkpoints):
                if key[0] == run_id:
                    self._state.checkpoints.pop(key)
            for key in tuple(self._state.writes):
                if key[0] == run_id:
                    self._state.writes.pop(key)
        _audit(record, "run_deleted", "committed")

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
        return self.checkpointer.config_specs

    def get_tuple(self, config: Any) -> Any:
        return self.checkpointer.get_tuple(config)

    def list(self, config: Any, **options: Any) -> Any:
        return self.checkpointer.list(config, **options)

    def put(
        self,
        config: Any,
        checkpoint: Any,
        metadata: Any,
        new_versions: Any,
    ) -> Any:
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
        self.checkpointer.put_writes(config, writes, task_id, task_path)

    def delete_thread(self, thread_id: str) -> None:
        self.checkpointer.delete_thread(thread_id)

    async def aget_tuple(self, config: Any) -> Any:
        return await self.checkpointer.aget_tuple(config)

    async def alist(self, config: Any, **options: Any) -> AsyncIterator[Any]:
        async for value in self.checkpointer.alist(config, **options):
            yield value

    async def aput(
        self,
        config: Any,
        checkpoint: Any,
        metadata: Any,
        new_versions: Any,
    ) -> Any:
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
        await self.checkpointer.aput_writes(
            config, writes, task_id, task_path
        )

    async def adelete_thread(self, thread_id: str) -> None:
        await self.checkpointer.adelete_thread(thread_id)

    def get_next_version(self, current: Any, channel: Any) -> Any:
        return self.checkpointer.get_next_version(current, channel)

    def __getattr__(self, name: str) -> Any:
        # Delegation makes the lifecycle-owned wrapper usable directly in
        # builder.compile(checkpointer=...) without hiding its ownership.
        return getattr(self.checkpointer, name)

    async def start(self) -> None:
        await _optional_async_call(self.checkpointer, "start")

    async def close(self) -> None:
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
        await _optional_async_call(self.session_service, "start")

    async def close(self) -> None:
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
    "RunStatus",
    "checkpoint_metadata",
]
