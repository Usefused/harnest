"""Redis-backed Harnest session, checkpoint, and continuation storage."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import importlib
import json
import secrets
import time
from typing import Any, Literal

from . import store_redis_scripts as scripts
from ._json import json_value
from .checkpoint import (
    A2ATaskConflictError,
    A2ATaskCursorError,
    A2ATaskPage,
    A2ATaskRecord,
    CheckpointConflictError,
    CheckpointRecord,
    CheckpointWrite,
    HarnestStore,
    PendingAction,
    RunRecord,
    RunScope,
    RunStatus,
    _require_checkpoint_scope,
    _require_a2a_list_options,
    _require_fields,
    _validate_same_run,
    _validate_transition,
)
from .continuation import (
    ContinuationConflictError,
    ContinuationFailure,
    ContinuationRecord,
    ProviderPendingContinuation,
    _require_page,
    _validated_resolution,
    audit_continuation,
    external_id_key,
)
from .durable import ResumeArtifact
from .logging import get_logger
from .runtime_contract import SessionConflictError, SessionRecord
from .session import SessionLease, _require_list_options


_SCHEMA_VERSION = "4"
_MIGRATABLE_SCHEMA_VERSIONS = frozenset({"1", "2", "3"})
_AUDIT = get_logger("store.audit")
_A2A_MISSING_TIMESTAMP_SCORE = -62_135_596_800_000_000
_A2A_MUTATION_RETRIES = 8


class RedisStore(HarnestStore):
    """Combined Redis store with atomic CAS and renewable execution leases.

    Production durability depends on Redis persistence, replication, and
    failover configuration; Postgres remains the reference durable backend.
    """

    def __init__(
        self,
        url: str,
        *,
        prefix: str = "harnest",
        session_ttl_seconds: int | None = None,
        checkpoint_ttl_seconds: int = 604_800,
        lease_seconds: int = 60,
        lease_wait_seconds: float = 30,
        client_options: Mapping[str, Any] | None = None,
        _client: Any = None,
    ) -> None:
        """Configure bounded leases, data retention, and injected-client ownership."""

        _require_text(url, "url")
        _require_text(prefix, "prefix")
        _require_positive(checkpoint_ttl_seconds, "checkpoint_ttl_seconds")
        _require_positive(lease_seconds, "lease_seconds")
        _require_optional_positive(session_ttl_seconds, "session_ttl_seconds")
        if lease_wait_seconds < 0:
            raise ValueError("lease_wait_seconds cannot be negative")
        self._url = url
        self._prefix = prefix.rstrip(":")
        # Redis Cluster requires every key touched by one Lua script to share a
        # slot. A per-store hash tag preserves atomicity across run/index keys.
        self._slot = "{" + _digest(self._prefix)[:16] + "}"
        self._session_ttl = session_ttl_seconds or 0
        self._checkpoint_ttl = checkpoint_ttl_seconds
        self._lease_ms = lease_seconds * 1000
        self._lease_wait = lease_wait_seconds
        self._client_options = dict(client_options or {})
        self._client = _client
        self._owns_client = _client is None

    async def start(self) -> None:
        """Open the client and establish the compatible schema marker."""

        if self._client is None:
            self._client = _create_client(self._url, self._client_options)
        schema_key = self._key("schema")
        await self._client.set(schema_key, _SCHEMA_VERSION, nx=True)
        version = _text(await self._client.get(schema_key))
        if version in _MIGRATABLE_SCHEMA_VERSIONS:
            # These versions are additive: existing records need no rewrite;
            # new continuation fields and keys appear only on their first use.
            await self._client.set(schema_key, _SCHEMA_VERSION)
            version = _text(await self._client.get(schema_key))
        if version != _SCHEMA_VERSION:
            raise RuntimeError(
                f"unsupported Harnest Redis schema {version!r}; expected {_SCHEMA_VERSION}"
            )

    async def create(
        self, *, session_id: str, user_id: str, state: Mapping[str, Any]
    ) -> SessionRecord:
        """Create one tenant-scoped session with an atomic Lua mutation."""

        _require_text(session_id, "session_id")
        _require_text(user_id, "user_id")
        record = SessionRecord(
            id=session_id,
            user_id=user_id,
            state=json_value(state),
            created_at=_timestamp(),
            updated_at=_timestamp(),
        )
        keys = (self._session_key(user_id, session_id), self._user_key(user_id))
        try:
            created = await self._eval(
                scripts.CREATE_SESSION,
                keys,
                (_session_dump(record), session_id, self._session_ttl),
            )
        except Exception:
            _audit("session.create", "user", "failed", "redis")
            raise
        if not created:
            _audit("session.create", "user", "failed", "redis")
            raise SessionConflictError("session already exists")
        _audit("session.create", "user", "committed", "redis")
        return record

    async def get(
        self, *, session_id: str, user_id: str
    ) -> SessionRecord | None:
        raw = await self._require_client().get(
            self._session_key(user_id, session_id)
        )
        return None if raw is None else _session_load(raw)

    async def list(
        self,
        *,
        user_id: str,
        after: str | None = None,
        limit: int | None = None,
    ) -> Sequence[SessionRecord]:
        """Read one lexicographically ordered keyset page with one MGET."""

        _require_text(user_id, "user_id")
        _require_list_options(after, limit)
        client = self._require_client()
        if after is None and limit is None:
            session_ids = await client.zrange(self._user_key(user_id), 0, -1)
        else:
            minimum = "-" if after is None else f"({after}"
            options = {} if limit is None else {"start": 0, "num": limit}
            session_ids = await client.zrangebylex(
                self._user_key(user_id), minimum, "+", **options
            )
        if not session_ids:
            return ()
        keys = [self._session_key(user_id, _text(item)) for item in session_ids]
        # MGET keeps this set-based even when a user owns many sessions.
        values = await client.mget(keys)
        return tuple(_session_load(raw) for raw in values if raw is not None)

    async def put_a2a_task(self, record: A2ATaskRecord) -> A2ATaskRecord:
        """Atomically update one A2A record and all of its query indexes."""

        try:
            for _attempt in range(_A2A_MUTATION_RETRIES):
                stored = await self._put_a2a_task_once(record)
                if stored is not None:
                    _audit("a2a.task_saved", "agent", "committed", "redis")
                    return stored
        except Exception:
            _audit("a2a.task_saved", "agent", "failed", "redis")
            raise
        _audit("a2a.task_saved", "agent", "failed", "redis")
        raise A2ATaskConflictError("A2A task changed during persistence")

    async def get_a2a_task(
        self, *, application_id: str, user_id: str, task_id: str
    ) -> A2ATaskRecord | None:
        """Read one task through its digested application and owner key."""

        _require_fields(application_id, user_id, task_id)
        raw = await self._require_client().get(
            self._a2a_task_key(application_id, user_id, task_id)
        )
        return None if raw is None else _a2a_task_load(raw)

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
        """Use one prefiltered sorted index and one batched record read."""

        _require_a2a_list_options(
            application_id,
            user_id,
            context_id,
            status,
            status_timestamp_after,
            cursor_task_id,
            limit,
        )
        index = self._a2a_task_index(
            application_id, user_id, context_id=context_id, status=status
        )
        minimum = _a2a_minimum_score(status_timestamp_after)
        client = self._require_client()
        offset = await _redis_a2a_cursor_offset(
            client, index, cursor_task_id, minimum
        )
        total = await client.zcount(index, minimum, "+inf")
        task_ids = await client.zrevrangebyscore(
            index, "+inf", minimum, start=offset, num=limit
        )
        records = await self._load_a2a_task_page(
            application_id, user_id, task_ids
        )
        return A2ATaskPage(records, int(total))

    async def delete_a2a_task(
        self, *, application_id: str, user_id: str, task_id: str
    ) -> bool:
        """Delete a record and every selected-list index with optimistic CAS."""

        _require_fields(application_id, user_id, task_id)
        try:
            for _attempt in range(_A2A_MUTATION_RETRIES):
                deleted = await self._delete_a2a_task_once(
                    application_id, user_id, task_id
                )
                if deleted is not None:
                    if deleted:
                        _audit(
                            "a2a.task_deleted", "user", "committed", "redis"
                        )
                    return deleted
        except Exception:
            _audit("a2a.task_deleted", "user", "failed", "redis")
            raise
        _audit("a2a.task_deleted", "user", "failed", "redis")
        raise A2ATaskConflictError("A2A task changed during deletion")

    async def _put_a2a_task_once(
        self, record: A2ATaskRecord
    ) -> A2ATaskRecord | None:
        """Attempt one index-safe compare-and-swap for an A2A snapshot."""

        client = self._require_client()
        record_key = self._a2a_task_key(
            record.application_id, record.user_id, record.task_id
        )
        current_raw = await client.get(record_key)
        current = None if current_raw is None else _a2a_task_load(current_raw)
        if current is not None and current.context_id != record.context_id:
            raise A2ATaskConflictError(
                "A2A task context cannot change after creation"
            )
        stored = replace(
            record,
            created_at=record.created_at if current is None else current.created_at,
            updated_at=_timestamp(),
        )
        old = stored if current is None else current
        keys = self._a2a_mutation_keys(stored, old)
        result = await self._eval(
            scripts.PUT_A2A_TASK,
            keys,
            (
                "" if current_raw is None else _text(current_raw),
                _a2a_task_dump(stored),
                stored.task_id,
                _a2a_score(stored.status_timestamp),
            ),
        )
        return stored if _text(result) == "ok" else None

    async def _delete_a2a_task_once(
        self, application_id: str, user_id: str, task_id: str
    ) -> bool | None:
        """Attempt one record-matched delete so recreated tasks are preserved."""

        client = self._require_client()
        record_key = self._a2a_task_key(application_id, user_id, task_id)
        current_raw = await client.get(record_key)
        if current_raw is None:
            return False
        current = _a2a_task_load(current_raw)
        keys = self._a2a_mutation_keys(current, current)
        result = _text(
            await self._eval(
                scripts.DELETE_A2A_TASK,
                keys,
                (_text(current_raw), task_id),
            )
        )
        return None if result == "conflict" else result == "deleted"

    async def _load_a2a_task_page(
        self, application_id: str, user_id: str, task_ids: Sequence[Any]
    ) -> tuple[A2ATaskRecord, ...]:
        """Load an indexed page in one MGET and reject broken index references."""

        if not task_ids:
            return ()
        keys = [
            self._a2a_task_key(application_id, user_id, _text(task_id))
            for task_id in task_ids
        ]
        values = await self._require_client().mget(keys)
        if any(raw is None for raw in values):
            raise RuntimeError("A2A task index references a missing record")
        return tuple(_a2a_task_load(raw) for raw in values)

    async def update(
        self,
        *,
        session_id: str,
        user_id: str,
        state_delta: Mapping[str, Any],
    ) -> SessionRecord | None:
        """Atomically merge state while preserving session expiry policy."""

        try:
            async with self._mutation_lock(user_id, session_id):
                raw = await self._eval(
            scripts.UPDATE_SESSION,
                    (
                        self._session_key(user_id, session_id),
                        self._user_key(user_id),
                    ),
                    (_json_dump(state_delta), _timestamp(), self._session_ttl),
                )
        except Exception:
            _audit("session.update", "agent", "failed", "redis")
            raise
        if raw is None:
            return None
        _audit("session.update", "agent", "committed", "redis")
        return _session_load(raw)

    async def delete(self, *, session_id: str, user_id: str) -> bool:
        """Delete a session and its user index entry atomically."""

        try:
            async with self._mutation_lock(user_id, session_id):
                deleted = await self._eval(
                    scripts.DELETE_SESSION,
                    (
                        self._session_key(user_id, session_id),
                        self._user_key(user_id),
                    ),
                    (session_id,),
                )
        except Exception:
            _audit("session.delete", "user", "failed", "redis")
            raise
        if not deleted:
            return False
        _audit("session.delete", "user", "committed", "redis")
        return True

    @asynccontextmanager
    async def acquire(
        self, *, session_id: str, user_id: str
    ) -> AsyncIterator[SessionLease]:
        """Hold and renew a token-bound distributed lease for one execution."""

        lock_key = self._lease_key(user_id, session_id)
        token = secrets.token_hex(16)
        await self._acquire_lock(lock_key, token)
        stop = asyncio.Event()
        renewer = asyncio.create_task(self._renew_lock(lock_key, token, stop))
        try:
            record = await self.get(session_id=session_id, user_id=user_id)
            if record is None:
                raise KeyError("session not found")
            yield _RedisLease(self, record, lock_key, token)
        finally:
            stop.set()
            renewer.cancel()
            with suppress(asyncio.CancelledError):
                await renewer
            await self._eval(scripts.COMPARE_DELETE, (lock_key,), (token,))

    async def begin_run(
        self,
        *,
        application_id: str,
        user_id: str,
        session_id: str,
        run_id: str,
        framework: Literal["adk", "langgraph"],
    ) -> RunRecord:
        """Create an idempotent run while allowing one active run per session."""

        _require_fields(application_id, user_id, session_id, run_id)
        record = RunRecord(
            application_id, user_id, session_id, run_id, framework
        )
        active_key = self._active_key(application_id, user_id, session_id)
        try:
            raw = await self._eval(
                scripts.BEGIN_RUN,
                (self._run_key(run_id), active_key),
                (_run_dump(record), run_id, self._checkpoint_ttl),
            )
        except Exception:
            _audit("checkpoint.run_started", "agent", "failed", "redis")
            raise
        if raw is None:
            _audit("checkpoint.run_started", "agent", "failed", "redis")
            raise CheckpointConflictError("session already has an active run")
        stored = _run_load(raw)
        try:
            _validate_same_run(
                stored, application_id, user_id, session_id, framework
            )
        except CheckpointConflictError:
            _audit("checkpoint.run_started", "agent", "failed", "redis")
            raise
        _audit("checkpoint.run_started", "agent", "committed", "redis")
        return stored

    async def get_run(self, *, scope: RunScope) -> RunRecord | None:
        raw = await self._require_client().get(self._run_key(scope.run_id))
        if raw is None:
            return None
        record = _run_load(raw)
        return record if record.scope == scope else None

    async def get_checkpoint(
        self,
        *,
        scope: RunScope,
        checkpoint_id: str | None = None,
        namespace: str = "",
    ) -> CheckpointRecord | None:
        """Resolve an exact or newest checkpoint through its Redis index."""

        if await self.get_run(scope=scope) is None:
            return None
        client = self._require_client()
        if checkpoint_id is None:
            members = await client.zrevrange(
                self._checkpoint_index(scope.run_id, namespace), 0, 0
            )
            if not members:
                return None
            raw = await client.get(members[0])
        else:
            raw = await client.get(
                self._checkpoint_key(scope.run_id, namespace, checkpoint_id)
            )
        return None if raw is None else _checkpoint_load(raw)

    async def list_checkpoints(
        self,
        *,
        scope: RunScope,
        namespace: str | None = None,
        before: str | None = None,
        limit: int = 20,
    ) -> AsyncIterator[CheckpointRecord]:
        """Read bounded checkpoint history from sorted-set indexes."""

        _require_limit(limit)
        if await self.get_run(scope=scope) is None:
            return
        index = self._checkpoint_index(scope.run_id, namespace)
        cursor = self._checkpoint_cursor(scope.run_id, namespace)
        maximum: Any = "+inf"
        if before is not None:
            score = await self._require_client().hget(cursor, before)
            if score is None:
                return
            maximum = f"({_text(score)}"
        members = await self._require_client().zrevrangebyscore(
            index, maximum, "-inf", start=0, num=limit
        )
        if not members:
            return
        values = await self._require_client().mget(members)
        for raw in values:
            if raw is not None:
                yield _checkpoint_load(raw)

    async def put(
        self,
        checkpoint: CheckpointRecord,
        *,
        scope: RunScope,
        expected_revision: int | None,
    ) -> CheckpointRecord:
        """Persist checkpoint data and revision indexes with one Lua CAS."""

        _require_checkpoint_scope(checkpoint, scope)
        await self._require_active_run(scope)
        checkpoint_key = self._checkpoint_key(
            checkpoint.run_id, checkpoint.namespace, checkpoint.checkpoint_id
        )
        keys = (
            self._run_key(checkpoint.run_id),
            checkpoint_key,
            self._checkpoint_index(checkpoint.run_id, None),
            self._checkpoint_index(checkpoint.run_id, checkpoint.namespace),
            self._checkpoint_cursor(checkpoint.run_id, None),
            self._checkpoint_cursor(checkpoint.run_id, checkpoint.namespace),
            self._key("checkpoint-sequence", _digest(checkpoint.run_id)),
        )
        expected = -1 if expected_revision is None else expected_revision
        try:
            result = await self._eval(
                scripts.PUT_CHECKPOINT,
                keys,
                (
                    expected,
                    _checkpoint_dump(checkpoint),
                    checkpoint.checkpoint_id,
                    self._checkpoint_ttl,
                ),
            )
        except Exception:
            _audit("checkpoint.saved", "agent", "failed", "redis")
            raise
        try:
            stored = _checkpoint_result(result)
        except (KeyError, CheckpointConflictError):
            _audit("checkpoint.saved", "agent", "failed", "redis")
            raise
        _audit("checkpoint.saved", "agent", "committed", "redis")
        return stored

    async def put_writes(
        self,
        *,
        scope: RunScope,
        checkpoint_id: str,
        writes: Sequence[CheckpointWrite],
    ) -> None:
        """Add pending writes idempotently with one Lua operation."""

        if not writes:
            return
        await self._require_active_run(scope)
        arguments: list[Any] = []
        for value in writes:
            arguments.extend((_write_field(value), _write_dump(value)))
        arguments.append(self._checkpoint_ttl)
        try:
            result = await self._eval(
                scripts.PUT_WRITES,
                (
                    self._run_key(scope.run_id),
                    self._writes_key(scope.run_id, checkpoint_id),
                ),
                arguments,
            )
        except Exception:
            _audit("checkpoint.writes_saved", "agent", "failed", "redis")
            raise
        _raise_checkpoint_result(_text(result))
        _audit("checkpoint.writes_saved", "agent", "committed", "redis")

    async def get_writes(
        self, *, scope: RunScope, checkpoint_id: str
    ) -> Sequence[CheckpointWrite]:
        if await self.get_run(scope=scope) is None:
            return ()
        values = await self._require_client().hvals(
            self._writes_key(scope.run_id, checkpoint_id)
        )
        return tuple(_write_load(value) for value in values)

    async def get_writes_batch(
        self, *, scope: RunScope, checkpoint_ids: Sequence[str]
    ) -> Mapping[str, Sequence[CheckpointWrite]]:
        """Fetch several pending-write hashes in one Redis round trip."""

        if not checkpoint_ids or await self.get_run(scope=scope) is None:
            return {}
        keys = [
            self._writes_key(scope.run_id, value) for value in checkpoint_ids
        ]
        result = await self._eval(
            scripts.GET_WRITES_BATCH, keys, checkpoint_ids
        )
        grouped: dict[str, Sequence[CheckpointWrite]] = {}
        for index in range(0, len(result), 2):
            checkpoint_id = _text(result[index])
            values = _json_load(result[index + 1])
            grouped[checkpoint_id] = tuple(_write_load(value) for value in values)
        return grouped

    async def suspend_continuation(
        self, *, record: ContinuationRecord, external_id: str
    ) -> ContinuationRecord:
        """Atomically persist a provider wait and move its run to waiting."""

        keys = (
            self._run_key(record.run_id),
            self._continuation_key(record.continuation_id),
            self._continuation_external_key(
                record.application_id, record.provider, external_id
            ),
            self._continuation_pending_key(record.application_id, record.provider),
        )
        arguments = (
            _continuation_dump(record, external_id),
            record.continuation_id,
            _json_dump(asdict(record.pending_action)),
            _timestamp(),
            self._checkpoint_ttl,
        )
        try:
            stored = _continuation_result(
                await self._eval(scripts.SUSPEND_CONTINUATION, keys, arguments)
            )
        except Exception:
            audit_continuation("suspended", "failed", "redis")
            raise
        audit_continuation("suspended", "committed", "redis")
        return stored.record

    async def get_continuation(
        self, *, scope: RunScope, continuation_id: str
    ) -> ContinuationRecord | None:
        """Read one private record only when all ownership fields match."""

        raw, run_raw = await self._require_client().mget(
            (
                self._continuation_key(continuation_id),
                self._run_key(scope.run_id),
            )
        )
        if raw is None or run_raw is None:
            return None
        record = _continuation_load(raw).record
        run = _run_load(run_raw)
        return record if record.scope == scope and run.scope == scope else None

    async def get_provider_continuation(
        self, *, scope: RunScope, provider: str, continuation_id: str
    ) -> ProviderPendingContinuation | None:
        """Load an exact provider envelope only while its owned run exists."""

        _require_fields(provider, continuation_id)
        raw, run_raw = await self._require_client().mget(
            (
                self._continuation_key(continuation_id),
                self._run_key(scope.run_id),
            )
        )
        if raw is None or run_raw is None:
            return None
        pending = _continuation_load(raw)
        run = _run_load(run_raw)
        if (
            pending.record.scope != scope
            or run.scope != scope
            or pending.record.provider != provider
        ):
            return None
        return pending

    async def get_continuation_by_external_id(
        self, *, application_id: str, provider: str, external_id: str
    ) -> ProviderPendingContinuation | None:
        """Resolve a callback through its provider-owned Redis index."""

        _require_fields(application_id, provider, external_id)
        external_key = self._continuation_external_key(
            application_id, provider, external_id
        )
        continuation_id = await self._require_client().get(external_key)
        if continuation_id is None:
            return None
        raw = await self._require_client().get(
            self._continuation_key(_text(continuation_id))
        )
        return None if raw is None else _continuation_load(raw)

    async def list_pending_continuations(
        self,
        *,
        application_id: str,
        provider: str,
        after: str | None = None,
        limit: int = 100,
    ) -> Sequence[ProviderPendingContinuation]:
        """Load one indexed provider page with a single batched value read."""

        _require_fields(application_id, provider)
        _require_page(after, limit)
        minimum = "-" if after is None else f"({after}"
        client = self._require_client()
        continuation_ids = await client.zrangebylex(
            self._continuation_pending_key(application_id, provider),
            minimum,
            "+",
            start=0,
            num=limit,
        )
        if not continuation_ids:
            return ()
        values = await client.mget(
            [
                self._continuation_key(_text(continuation_id))
                for continuation_id in continuation_ids
            ]
        )
        return tuple(
            _continuation_load(raw) for raw in values if raw is not None
        )

    async def resolve_continuation(
        self,
        *,
        scope: RunScope,
        provider: str,
        external_id: str,
        schema_id: str,
        result: Any = None,
        failure: ContinuationFailure | None = None,
    ) -> ContinuationRecord:
        """Resolve an indexed provider callback with an atomic status CAS."""

        result, failure = _validated_resolution(result, failure)
        external_key = self._continuation_external_key(
            scope.application_id, provider, external_id
        )
        continuation_id = await self._require_client().get(external_key)
        if continuation_id is None:
            audit_continuation("resolved", "failed", "redis")
            raise ContinuationConflictError("continuation state changed")
        continuation_id = _text(continuation_id)
        keys = (
            self._continuation_key(continuation_id),
            external_key,
            self._continuation_pending_key(scope.application_id, provider),
            self._run_key(scope.run_id),
        )
        arguments = (
            continuation_id,
            scope.application_id,
            scope.user_id,
            scope.session_id,
            scope.run_id,
            provider,
            schema_id,
            "failed" if failure is not None else "completed",
            _json_dump(result),
            _json_dump(
                None
                if failure is None
                else {"code": failure.code, "retryable": failure.retryable}
            ),
            _timestamp(),
            self._checkpoint_ttl,
        )
        try:
            resolved = _continuation_result(
                await self._eval(scripts.RESOLVE_CONTINUATION, keys, arguments)
            )
        except Exception:
            audit_continuation("resolved", "failed", "redis")
            raise
        audit_continuation("resolved", "committed", "redis")
        return resolved.record

    async def claim_continuation(
        self,
        *,
        scope: RunScope,
        provider: str,
        continuation_id: str,
        expected_revision: int,
    ) -> ContinuationRecord:
        """Claim a provider outcome and resume its run with one Lua CAS."""

        keys = (
            self._continuation_key(continuation_id),
            self._run_key(scope.run_id),
        )
        arguments = (
            scope.application_id,
            scope.user_id,
            scope.session_id,
            scope.run_id,
            provider,
            expected_revision,
            _timestamp(),
            self._checkpoint_ttl,
        )
        try:
            claimed = _continuation_result(
                await self._eval(scripts.CLAIM_CONTINUATION, keys, arguments)
            )
        except Exception:
            audit_continuation("claimed", "failed", "redis")
            raise
        audit_continuation("claimed", "committed", "redis")
        return claimed.record

    async def cancel_continuation(
        self,
        *,
        scope: RunScope,
        provider: str,
        continuation_id: str,
        expected_revision: int,
        failure: ContinuationFailure,
    ) -> ContinuationRecord:
        """Cancel one exact waiting run and fail its continuation in one script."""

        if not isinstance(failure, ContinuationFailure):
            audit_continuation("cancelled", "failed", "redis")
            raise TypeError("continuation cancellation failure is invalid")
        keys = (
            self._continuation_key(continuation_id),
            self._run_key(scope.run_id),
            self._continuation_pending_key(scope.application_id, provider),
            self._active_key(
                scope.application_id, scope.user_id, scope.session_id
            ),
        )
        arguments = (
            continuation_id,
            scope.application_id,
            scope.user_id,
            scope.session_id,
            scope.run_id,
            provider,
            expected_revision,
            _failure_dump(failure),
            _timestamp(),
            self._checkpoint_ttl,
        )
        try:
            cancelled = _continuation_result(
                await self._eval(
                    scripts.CANCEL_CONTINUATION, keys, arguments
                )
            )
        except Exception:
            audit_continuation("cancelled", "failed", "redis")
            raise
        audit_continuation("cancelled", "committed", "redis")
        return cancelled.record

    async def arm_continuation(
        self,
        *,
        scope: RunScope,
        provider: str,
        continuation_id: str,
        expected_revision: int,
    ) -> ContinuationRecord:
        """Arm one persisted wait after the framework commits its checkpoint."""

        arguments = (
            scope.application_id,
            scope.user_id,
            scope.session_id,
            scope.run_id,
            provider,
            expected_revision,
            _timestamp(),
            self._checkpoint_ttl,
        )
        try:
            armed = _continuation_result(
                await self._eval(
                    scripts.ARM_CONTINUATION,
                    (self._continuation_key(continuation_id),),
                    arguments,
                )
            )
        except Exception:
            audit_continuation("armed", "failed", "redis")
            raise
        audit_continuation("armed", "committed", "redis")
        return armed.record

    async def transition(
        self,
        *,
        scope: RunScope,
        expected_status: RunStatus,
        status: RunStatus,
        pending_action: PendingAction | None = None,
    ) -> RunRecord:
        """Apply one status compare-and-swap and release terminal ownership."""

        _validate_transition(expected_status, status, pending_action)
        current = await self.get_run(scope=scope)
        if current is None:
            _audit("checkpoint.transition", "agent", "failed", "redis")
            raise KeyError("checkpoint run not found")
        active_key = self._active_key(
            current.application_id, current.user_id, current.session_id
        )
        try:
            result = await self._eval(
                scripts.TRANSITION_RUN,
                (self._run_key(scope.run_id), active_key),
                (
                    expected_status,
                    status,
                    _json_dump(None if pending_action is None else asdict(pending_action)),
                    _timestamp(),
                    scope.run_id,
                    self._checkpoint_ttl,
                ),
            )
        except Exception:
            _audit("checkpoint.transition", "agent", "failed", "redis")
            raise
        try:
            record = _run_result(result)
        except (KeyError, CheckpointConflictError):
            _audit("checkpoint.transition", "agent", "failed", "redis")
            raise
        _audit("checkpoint.transition", "agent", "committed", "redis")
        return record

    async def _require_active_run(self, scope: RunScope) -> RunRecord:
        record = await self.get_run(scope=scope)
        if record is None:
            raise KeyError("checkpoint run not found")
        if record.status not in {"running", "waiting"}:
            raise CheckpointConflictError("checkpoint run is already terminal")
        return record

    async def delete_run(self, *, scope: RunScope) -> None:
        """Make run data unreachable immediately and let private blobs expire."""

        current = await self.get_run(scope=scope)
        if current is None:
            return
        active = self._active_key(
            current.application_id, current.user_id, current.session_id
        )
        keys = [
            self._run_key(scope.run_id),
            active,
            self._checkpoint_index(scope.run_id, None),
            self._checkpoint_cursor(scope.run_id, None),
            self._key("checkpoint-sequence", _digest(scope.run_id)),
        ]
        arguments = [scope.run_id]
        if current.pending_action is not None and current.pending_action.type == "external_continuation":
            raw = await self._require_client().get(
                self._continuation_key(current.pending_action.action_id)
            )
            if raw is not None:
                pending = _continuation_load(raw)
                keys.extend(
                    (
                        self._continuation_key(pending.record.continuation_id),
                        self._continuation_external_key(
                            scope.application_id,
                            pending.record.provider,
                            pending.external_id,
                        ),
                        self._continuation_pending_key(
                            scope.application_id, pending.record.provider
                        ),
                    )
                )
                arguments.append(pending.record.continuation_id)
        try:
            await self._eval(scripts.DELETE_RUN, keys, arguments)
        except Exception:
            _audit("checkpoint.run_deleted", "user", "failed", "redis")
            raise
        # Checkpoint and write blobs expire independently. Removing the run and
        # indexes makes them unreachable immediately without a blocking key scan.
        _audit("checkpoint.run_deleted", "user", "committed", "redis")

    async def close(self) -> None:
        """Close only clients created and owned by this store."""

        client, self._client = self._client, None
        if client is not None and self._owns_client:
            close = getattr(client, "aclose", None) or getattr(client, "close", None)
            if callable(close):
                result = close()
                if hasattr(result, "__await__"):
                    await result

    async def _acquire_lock(self, key: str, token: str) -> None:
        """Wait only for the configured bound when acquiring a session lease."""

        deadline = time.monotonic() + self._lease_wait
        while True:
            acquired = await self._require_client().set(
                key, token, nx=True, px=self._lease_ms
            )
            if acquired:
                return
            if time.monotonic() >= deadline:
                raise SessionConflictError("session execution lease is busy")
            await asyncio.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    async def _renew_lock(
        self, key: str, token: str, stop: asyncio.Event
    ) -> None:
        """Renew a lease only while its original ownership token still matches."""

        interval = max(0.1, self._lease_ms / 3000)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                renewed = await self._eval(
                    scripts.COMPARE_EXPIRE, (key,), (token, self._lease_ms)
                )
                if not renewed:
                    return

    @asynccontextmanager
    async def _mutation_lock(
        self, user_id: str, session_id: str
    ) -> AsyncIterator[None]:
        """Serialize short session mutations without starting a renewer task."""

        key = self._lease_key(user_id, session_id)
        token = secrets.token_hex(16)
        await self._acquire_lock(key, token)
        try:
            yield
        finally:
            await self._eval(scripts.COMPARE_DELETE, (key,), (token,))

    async def _eval(
        self, script: str, keys: Sequence[str], arguments: Sequence[Any]
    ) -> Any:
        return await self._require_client().eval(
            script, len(keys), *keys, *arguments
        )

    def _require_client(self) -> Any:
        if self._client is None:
            raise RuntimeError("RedisStore.start() must be called first")
        return self._client

    def _key(self, *parts: str) -> str:
        return ":".join((self._prefix, self._slot, *parts))

    def _session_key(self, user_id: str, session_id: str) -> str:
        return self._key("session", _digest(user_id), _digest(session_id))

    def _user_key(self, user_id: str) -> str:
        return self._key("sessions", _digest(user_id))

    def _lease_key(self, user_id: str, session_id: str) -> str:
        return self._key("lease", _digest(user_id), _digest(session_id))

    def _run_key(self, run_id: str) -> str:
        return self._key("run", _digest(run_id))

    def _active_key(self, application: str, user: str, session: str) -> str:
        return self._key("active", _digest(application, user, session))

    def _a2a_task_key(
        self, application_id: str, user_id: str, task_id: str
    ) -> str:
        """Keep application, owner, and public task identity out of Redis keys."""

        return self._key(
            "a2a-task",
            _digest(application_id),
            _digest(user_id),
            _digest(task_id),
        )

    def _a2a_task_index(
        self,
        application_id: str,
        user_id: str,
        *,
        context_id: str | None = None,
        status: int | None = None,
    ) -> str:
        """Select the prefiltered sorted index for one A2A query shape."""

        base = (
            "a2a-tasks",
            _digest(application_id),
            _digest(user_id),
        )
        if context_id is not None and status is not None:
            return self._key(
                *base, "context-status", _digest(context_id), str(status)
            )
        if context_id is not None:
            return self._key(*base, "context", _digest(context_id))
        if status is not None:
            return self._key(*base, "status", str(status))
        return self._key(*base, "all")

    def _a2a_mutation_keys(
        self, record: A2ATaskRecord, old: A2ATaskRecord
    ) -> tuple[str, ...]:
        """Supply every old and new index key to the cluster-safe Lua mutation."""

        application_id, user_id = record.application_id, record.user_id
        return (
            self._a2a_task_key(application_id, user_id, record.task_id),
            self._a2a_task_index(application_id, user_id),
            self._a2a_task_index(
                application_id, user_id, context_id=record.context_id
            ),
            self._a2a_task_index(
                application_id, user_id, status=record.status
            ),
            self._a2a_task_index(
                application_id,
                user_id,
                context_id=record.context_id,
                status=record.status,
            ),
            self._a2a_task_index(
                application_id, user_id, context_id=old.context_id
            ),
            self._a2a_task_index(application_id, user_id, status=old.status),
            self._a2a_task_index(
                application_id,
                user_id,
                context_id=old.context_id,
                status=old.status,
            ),
        )

    def _checkpoint_key(
        self, run_id: str, namespace: str, checkpoint_id: str
    ) -> str:
        return self._key(
            "checkpoint", _digest(run_id), _digest(namespace, checkpoint_id)
        )

    def _checkpoint_index(self, run_id: str, namespace: str | None) -> str:
        scope = "all" if namespace is None else _digest(namespace)
        return self._key("checkpoints", _digest(run_id), scope)

    def _checkpoint_cursor(self, run_id: str, namespace: str | None) -> str:
        scope = "all" if namespace is None else _digest(namespace)
        return self._key("checkpoint-cursors", _digest(run_id), scope)

    def _writes_key(self, run_id: str, checkpoint_id: str) -> str:
        return self._key("writes", _digest(run_id), _digest(checkpoint_id))

    def _continuation_key(self, continuation_id: str) -> str:
        """Keep the opaque public id out of the Redis key namespace."""

        return self._key("continuation", _digest(continuation_id))

    def _continuation_external_key(
        self, application_id: str, provider: str, external_id: str
    ) -> str:
        """Index provider callbacks without exposing their external identifier."""

        return self._key(
            "continuation-external",
            _digest(application_id),
            external_id_key(provider, external_id),
        )

    def _continuation_pending_key(
        self, application_id: str, provider: str
    ) -> str:
        """Scope reconciliation indexes to a host-bound application/provider pair."""

        return self._key(
            "continuations", _digest(application_id), _digest(provider)
        )


class _RedisLease:
    def __init__(
        self,
        store: RedisStore,
        record: SessionRecord,
        lock_key: str,
        token: str,
    ) -> None:
        self._store = store
        self._record = record
        self._lock_key = lock_key
        self._token = token

    @property
    def record(self) -> SessionRecord:
        return self._record

    async def patch_state(self, delta: Mapping[str, Any]) -> SessionRecord:
        return await self.replace_state(
            {**dict(self._record.state), **json_value(delta)}
        )

    async def replace_state(self, state: Mapping[str, Any]) -> SessionRecord:
        """Replace leased state only if the execution still owns the token."""

        updated = replace(self._record, state=json_value(state))
        return await self._replace_record(updated)

    async def replace_application_data(
        self, data: Mapping[str, Any]
    ) -> SessionRecord:
        """Replace app data without mixing it into native framework state."""

        updated = replace(self._record, application_data=json_value(data))
        return await self._replace_record(updated)

    async def _replace_record(self, updated: SessionRecord) -> SessionRecord:
        """Use one token-checked write path for both protected session lanes."""

        updated = replace(updated, updated_at=_timestamp())
        try:
            raw = await self._store._eval(
                scripts.LEASE_REPLACE,
                (
                    self._store._session_key(
                        self._record.user_id, self._record.id
                    ),
                    self._lock_key,
                    self._store._user_key(self._record.user_id),
                ),
                (self._token, _session_dump(updated), self._store._session_ttl),
            )
        except Exception:
            _audit("session.lease_update", "agent", "failed", "redis")
            raise
        if raw is None:
            _audit("session.lease_update", "agent", "failed", "redis")
            raise SessionConflictError("session execution lease was lost")
        self._record = _session_load(raw)
        _audit("session.lease_update", "agent", "committed", "redis")
        return self._record


def _create_client(url: str, options: Mapping[str, Any]) -> Any:
    """Import redis-py only when this backend is selected and create its client."""

    try:
        module = importlib.import_module("redis.asyncio")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "RedisStore requires the 'redis' package; install harnest[redis]"
        ) from exc
    return module.Redis.from_url(url, **dict(options))


def _session_dump(value: SessionRecord) -> str:
    return _json_dump(
        {
            "id": value.id,
            "user_id": value.user_id,
            "state": value.state,
            "application_data": value.application_data,
            "created_at": value.created_at,
            "updated_at": value.updated_at,
        }
    )


def _session_load(value: Any) -> SessionRecord:
    data = _json_load(value)
    # Records written before the protected lane was introduced remain readable.
    data.setdefault("application_data", {})
    return SessionRecord(**data)


def _a2a_task_dump(value: A2ATaskRecord) -> str:
    """Encode the exact protobuf bytes while keeping projections queryable."""

    data = asdict(value)
    data["payload"] = base64.b64encode(value.payload).decode("ascii")
    return _json_dump(data)


def _a2a_task_load(value: Any) -> A2ATaskRecord:
    """Reject corrupt protobuf encoding before returning durable task state."""

    data = _json_load(value)
    data["payload"] = base64.b64decode(data["payload"], validate=True)
    return A2ATaskRecord(**data)


def _a2a_score(value: str | None) -> int:
    """Project UTC timestamps to Redis' microsecond sorted-set score."""

    if value is None:
        return _A2A_MISSING_TIMESTAMP_SCORE
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(parsed.astimezone(timezone.utc).timestamp() * 1_000_000)


def _a2a_minimum_score(value: str | None) -> int | str:
    """Translate the inclusive timestamp filter to a Redis score bound."""

    return "-inf" if value is None else _a2a_score(value)


async def _redis_a2a_cursor_offset(
    client: Any,
    index: str,
    cursor_task_id: str | None,
    minimum: int | str,
) -> int:
    """Resolve an inclusive cursor rank and validate its timestamp filter."""

    if cursor_task_id is None:
        return 0
    rank = await client.zrevrank(index, cursor_task_id)
    score = await client.zscore(index, cursor_task_id)
    below_minimum = minimum != "-inf" and score is not None and score < minimum
    if rank is None or score is None or below_minimum:
        raise A2ATaskCursorError("A2A task cursor is not valid for this list")
    return int(rank)


def _run_dump(value: RunRecord) -> str:
    return _json_dump(asdict(value))


def _run_load(value: Any) -> RunRecord:
    """Restore the typed pending action at the Redis serialization boundary."""

    data = _json_load(value)
    pending = data.pop("pending_action", None)
    return RunRecord(
        **data,
        pending_action=None if pending is None else PendingAction(**pending),
    )


def _continuation_dump(
    value: ContinuationRecord, external_id: str
) -> str:
    """Store provider-only reconciliation data outside the portable action."""

    return _json_dump({"record": asdict(value), "external_id": external_id})


def _continuation_load(value: Any) -> ProviderPendingContinuation:
    """Restore a provider-private envelope and its typed failure category."""

    envelope = _json_load(value)
    data = envelope["record"]
    failure = data.pop("failure", None)
    resume = data.pop("resume", None)
    record = ContinuationRecord(
        **data,
        resume=None if resume is None else ResumeArtifact.from_mapping(resume),
        failure=None if failure is None else ContinuationFailure(**failure),
    )
    return ProviderPendingContinuation(record, envelope["external_id"])


def _failure_dump(value: ContinuationFailure) -> str:
    """Serialize only the bounded continuation failure classification."""

    return _json_dump({"code": value.code, "retryable": value.retryable})


def _checkpoint_dump(value: CheckpointRecord) -> str:
    """Encode opaque binary payloads without interpreting framework state."""

    data = asdict(value)
    for name in ("payload", "metadata", "versions"):
        data[name] = base64.b64encode(data[name]).decode("ascii")
    return _json_dump(data)


def _checkpoint_load(value: Any) -> CheckpointRecord:
    """Reject corrupt binary encoding before returning framework state."""

    data = _json_load(value)
    for name in ("payload", "metadata", "versions"):
        data[name] = base64.b64decode(data[name], validate=True)
    return CheckpointRecord(**data)


def _write_dump(value: CheckpointWrite) -> str:
    data = asdict(value)
    data["payload"] = base64.b64encode(data["payload"]).decode("ascii")
    return _json_dump(data)


def _write_load(value: Any) -> CheckpointWrite:
    data = _json_load(value)
    data["payload"] = base64.b64decode(data["payload"], validate=True)
    return CheckpointWrite(**data)


def _write_field(value: CheckpointWrite) -> str:
    return _digest(value.task_id, value.channel)


def _checkpoint_result(result: Any) -> CheckpointRecord:
    code = _text(result[0])
    _raise_checkpoint_result(code)
    return _checkpoint_load(result[1])


def _run_result(result: Any) -> RunRecord:
    code = _text(result[0])
    _raise_checkpoint_result(code)
    return _run_load(result[1])


def _continuation_result(result: Any) -> ProviderPendingContinuation:
    """Translate one atomic Lua result to the provider-private record."""

    code = _text(result[0])
    try:
        _raise_checkpoint_result(code)
    except (KeyError, CheckpointConflictError) as exc:
        raise ContinuationConflictError("continuation state changed") from exc
    return _continuation_load(result[1])


def _raise_checkpoint_result(code: str) -> None:
    """Translate atomic Lua outcomes into the portable checkpoint contract."""

    if code == "missing":
        raise KeyError("checkpoint run not found")
    if code == "terminal":
        raise CheckpointConflictError("checkpoint run is already terminal")
    if code == "conflict":
        raise CheckpointConflictError("checkpoint revision or status changed")
    if code != "ok":
        raise RuntimeError(f"unexpected Redis checkpoint result {code!r}")


def _json_dump(value: Any) -> str:
    return json.dumps(json_value(value), separators=(",", ":"), sort_keys=True)


def _json_load(value: Any) -> Any:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        return json.loads(value)
    return value


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _digest(*values: str) -> str:
    """Keep raw tenant and execution identifiers out of Redis key names."""

    encoded = "\0".join(values).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_text(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_positive(value: int, name: str) -> None:
    if value < 1:
        raise ValueError(f"{name} must be positive")


def _require_optional_positive(value: int | None, name: str) -> None:
    if value is not None:
        _require_positive(value, name)


def _require_limit(limit: int) -> None:
    if limit < 1:
        raise ValueError("checkpoint list limit must be positive")


def _audit(operation: str, trigger: str, outcome: str, backend: str) -> None:
    """Emit a payload-free signal for each attempted durable mutation."""

    # Redis keys and stored values may encode tenant or customer information.
    _AUDIT.info(
        operation,
        operation=operation,
        trigger=trigger,
        outcome=outcome,
        backend=backend,
    )


__all__ = ["RedisStore"]
