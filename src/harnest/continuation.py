"""Provider-neutral durable waits for work owned by external runtimes."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
import secrets
from typing import Any, Literal, Protocol, runtime_checkable

from ._json import json_value
from .checkpoint import PendingAction, RunScope
from .durable import ResumeArtifact
from .logging import get_logger


ContinuationStatus = Literal["pending", "completed", "failed", "claimed"]
ContinuationValidator = Callable[[Any], Any]
_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_SCHEMA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,255}$")
_AUDIT = get_logger("continuation.audit")


class ContinuationError(RuntimeError):
    """Base error for external continuation ownership and persistence."""


class ContinuationConflictError(ContinuationError):
    """Raised when a continuation compare-and-swap condition changed."""


class ContinuationValidationError(ContinuationError):
    """Raised before untrusted external output crosses the durable boundary."""


@dataclass(frozen=True, slots=True)
class ContinuationFailure:
    """A provider-defined failure category without exception or payload text."""

    code: str
    retryable: bool = False

    def __post_init__(self) -> None:
        _require_name(self.code, "failure code")
        if type(self.retryable) is not bool:
            raise ValueError("failure retryable must be a boolean")


@dataclass(frozen=True, slots=True)
class ContinuationRecord:
    """Private durable state for one externally completed agent wait."""

    continuation_id: str
    application_id: str
    user_id: str
    session_id: str
    run_id: str
    provider: str
    capability: str
    schema_id: str
    resume: ResumeArtifact | None = None
    status: ContinuationStatus = "pending"
    ready: bool = False
    revision: int = 0
    result: Any = None
    failure: ContinuationFailure | None = None
    created_at: str = field(default_factory=lambda: _timestamp())
    updated_at: str = field(default_factory=lambda: _timestamp())

    def __post_init__(self) -> None:
        """Reject ambiguous ownership or payload states before persistence."""

        _require_text(self.continuation_id, "continuation_id")
        for value, name in (
            (self.application_id, "application_id"),
            (self.user_id, "user_id"),
            (self.session_id, "session_id"),
            (self.run_id, "run_id"),
        ):
            _require_text(value, name)
        _require_name(self.provider, "provider")
        _require_name(self.capability, "capability")
        if not isinstance(self.schema_id, str) or not _SCHEMA.fullmatch(self.schema_id):
            raise ValueError("schema_id must be a stable non-empty identifier")
        if self.resume is not None and not isinstance(self.resume, ResumeArtifact):
            raise TypeError("continuation resume must be a ResumeArtifact")
        if type(self.ready) is not bool:
            raise ValueError("continuation ready must be a boolean")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("continuation revision must be a non-negative integer")
        _validate_record_payload(self.status, self.result, self.failure)

    @property
    def scope(self) -> RunScope:
        """Return the complete run ownership boundary for this wait."""

        return RunScope(
            self.application_id, self.user_id, self.session_id, self.run_id
        )

    @property
    def pending_action(self) -> PendingAction:
        """Expose only the opaque action needed by the portable run state."""

        return PendingAction(
            "external_continuation", self.continuation_id, self.capability
        )


@dataclass(frozen=True, slots=True)
class ProviderPendingContinuation:
    """A provider-private reconciliation item including its external identity."""

    record: ContinuationRecord
    external_id: str


@runtime_checkable
class ContinuationStore(Protocol):
    """Atomic persistence required by the host-bound provider facade."""

    async def suspend_continuation(
        self, *, record: ContinuationRecord, external_id: str
    ) -> ContinuationRecord: ...

    async def get_continuation(
        self, *, scope: RunScope, continuation_id: str
    ) -> ContinuationRecord | None: ...

    async def get_continuation_by_external_id(
        self, *, application_id: str, provider: str, external_id: str
    ) -> ProviderPendingContinuation | None: ...

    async def list_pending_continuations(
        self,
        *,
        application_id: str,
        provider: str,
        after: str | None = None,
        limit: int = 100,
    ) -> Sequence[ProviderPendingContinuation]: ...

    async def resolve_continuation(
        self,
        *,
        scope: RunScope,
        provider: str,
        external_id: str,
        schema_id: str,
        result: Any = None,
        failure: ContinuationFailure | None = None,
    ) -> ContinuationRecord: ...

    async def claim_continuation(
        self,
        *,
        scope: RunScope,
        provider: str,
        continuation_id: str,
        expected_revision: int,
    ) -> ContinuationRecord: ...

    async def arm_continuation(
        self,
        *,
        scope: RunScope,
        provider: str,
        continuation_id: str,
        expected_revision: int,
    ) -> ContinuationRecord: ...


class ContinuationProvider:
    """Application/provider-bound facade supplied to trusted plugin code."""

    def __init__(
        self,
        store: ContinuationStore,
        *,
        application_id: str,
        provider: str,
    ) -> None:
        """Bind authority so authored code cannot impersonate another provider."""

        _require_text(application_id, "application_id")
        _require_name(provider, "provider")
        self._store = store
        self._application_id = application_id
        self._provider = provider

    @property
    def name(self) -> str:
        """Return the low-cardinality provider identity bound by the host."""

        return self._provider

    async def suspend(
        self,
        *,
        user_id: str,
        session_id: str,
        run_id: str,
        external_id: str,
        capability: str,
        schema_id: str,
        resume: ResumeArtifact,
    ) -> ContinuationRecord:
        """Atomically persist a wait and transition its owned run to waiting."""

        _require_external_id(external_id)
        record = ContinuationRecord(
            continuation_id=secrets.token_urlsafe(24),
            application_id=self._application_id,
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            provider=self._provider,
            capability=capability,
            schema_id=schema_id,
            resume=resume,
        )
        return await self._store.suspend_continuation(
            record=record, external_id=external_id
        )

    async def lookup(self, external_id: str) -> ProviderPendingContinuation | None:
        """Resolve one provider-owned external identity with an indexed lookup."""

        _require_external_id(external_id)
        return await self._store.get_continuation_by_external_id(
            application_id=self._application_id,
            provider=self._provider,
            external_id=external_id,
        )

    async def get(
        self, *, user_id: str, session_id: str, run_id: str, continuation_id: str
    ) -> ContinuationRecord | None:
        """Read one continuation only through its complete ownership scope."""

        return await self._store.get_continuation(
            scope=self._scope(user_id, session_id, run_id),
            continuation_id=continuation_id,
        )

    async def list_pending(
        self, *, after: str | None = None, limit: int = 100
    ) -> Sequence[ProviderPendingContinuation]:
        """Read one bounded indexed page for provider startup reconciliation."""

        _require_page(after, limit)
        return await self._store.list_pending_continuations(
            application_id=self._application_id,
            provider=self._provider,
            after=after,
            limit=limit,
        )

    async def complete(
        self,
        *,
        user_id: str,
        session_id: str,
        run_id: str,
        external_id: str,
        schema_id: str,
        result: Any,
        validate: ContinuationValidator,
    ) -> ContinuationRecord:
        """Validate provider output before committing its private result."""

        normalized = _validated_result(result, validate)
        return await self._resolve(
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            external_id=external_id,
            schema_id=schema_id,
            result=normalized,
        )

    async def fail(
        self,
        *,
        user_id: str,
        session_id: str,
        run_id: str,
        external_id: str,
        schema_id: str,
        failure: ContinuationFailure,
    ) -> ContinuationRecord:
        """Commit a validated payload-free provider failure category."""

        if not isinstance(failure, ContinuationFailure):
            raise ContinuationValidationError("failure must be ContinuationFailure")
        return await self._resolve(
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            external_id=external_id,
            schema_id=schema_id,
            failure=failure,
        )

    async def claim(
        self,
        *,
        user_id: str,
        session_id: str,
        run_id: str,
        continuation_id: str,
        expected_revision: int,
    ) -> ContinuationRecord:
        """Claim a completed outcome once and return its private value."""

        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("expected_revision must be a positive integer")
        return await self._store.claim_continuation(
            scope=self._scope(user_id, session_id, run_id),
            provider=self._provider,
            continuation_id=continuation_id,
            expected_revision=expected_revision,
        )

    async def arm(self, record: ContinuationRecord) -> ContinuationRecord:
        """Mark a wait resumable only after its framework checkpoint is durable."""

        if record.application_id != self._application_id:
            raise ContinuationConflictError("continuation application changed")
        if record.provider != self._provider:
            raise ContinuationConflictError("continuation provider changed")
        return await self._store.arm_continuation(
            scope=record.scope,
            provider=self._provider,
            continuation_id=record.continuation_id,
            expected_revision=record.revision,
        )

    async def _resolve(
        self,
        *,
        user_id: str,
        session_id: str,
        run_id: str,
        external_id: str,
        schema_id: str,
        result: Any = None,
        failure: ContinuationFailure | None = None,
    ) -> ContinuationRecord:
        """Share the provider-bound lookup used by success and failure callbacks."""

        _require_external_id(external_id)
        return await self._store.resolve_continuation(
            scope=self._scope(user_id, session_id, run_id),
            provider=self._provider,
            external_id=external_id,
            schema_id=schema_id,
            result=result,
            failure=failure,
        )

    def _scope(self, user_id: str, session_id: str, run_id: str) -> RunScope:
        """Build a scope with the application authority fixed by the host."""

        return RunScope(self._application_id, user_id, session_id, run_id)


def continuation_schema_id(schema: Mapping[str, Any]) -> str:
    """Fingerprint a JSON schema without persisting its Python source type."""

    try:
        encoded = json.dumps(
            json_value(schema), separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContinuationValidationError("result schema must be JSON serializable") from exc
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def external_id_key(provider: str, external_id: str) -> str:
    """Build an indexed opaque key while keeping external ids out of indexes."""

    _require_name(provider, "provider")
    _require_external_id(external_id)
    return hashlib.sha256(f"{provider}\0{external_id}".encode("utf-8")).hexdigest()


def audit_continuation(
    operation: str, outcome: Literal["committed", "failed"], backend: str
) -> None:
    """Emit mutation outcome without continuation, tenant, or provider payloads."""

    _AUDIT.info(
        f"continuation.{operation}",
        operation=operation,
        trigger="agent",
        outcome=outcome,
        backend=backend,
    )


def _validated_result(value: Any, validate: ContinuationValidator) -> Any:
    """Normalize untrusted output only after the provider's schema accepts it."""

    if not callable(validate):
        raise ContinuationValidationError("result validator must be callable")
    try:
        return json_value(validate(value))
    except Exception as exc:
        raise ContinuationValidationError("external continuation result is invalid") from exc


def _validated_resolution(
    result: Any, failure: ContinuationFailure | None
) -> tuple[Any, ContinuationFailure | None]:
    """Protect low-level stores when called without the provider facade."""

    if failure is not None and not isinstance(failure, ContinuationFailure):
        raise ContinuationValidationError("failure must be ContinuationFailure")
    if failure is not None and result is not None:
        raise ContinuationValidationError("failure cannot include a result")
    try:
        return json_value(result), failure
    except Exception as exc:
        raise ContinuationValidationError(
            "external continuation result is invalid"
        ) from exc


def _validate_record_payload(
    status: ContinuationStatus,
    result: Any,
    failure: ContinuationFailure | None,
) -> None:
    """Keep result and failure lanes mutually exclusive and status-bound."""

    validators = {
        "pending": _validate_pending_payload,
        "completed": _validate_completed_payload,
        "failed": _validate_failed_payload,
        "claimed": _validate_claimed_payload,
    }
    validator = validators.get(status)
    if validator is None:
        raise ValueError("unsupported continuation status")
    validator(result, failure)


def _validate_pending_payload(
    result: Any, failure: ContinuationFailure | None
) -> None:
    """Prevent provider output from appearing before external completion."""

    if result is not None or failure is not None:
        raise ValueError("pending continuation cannot contain an outcome")


def _validate_completed_payload(
    _result: Any, failure: ContinuationFailure | None
) -> None:
    """Keep successful completion separate from provider failure metadata."""

    if failure is not None:
        raise ValueError("completed continuation cannot contain a failure")


def _validate_failed_payload(
    result: Any, failure: ContinuationFailure | None
) -> None:
    """Require one bounded failure category without an overlapping result."""

    if failure is None or result is not None:
        raise ValueError("failed continuation requires only a failure")


def _validate_claimed_payload(
    result: Any, failure: ContinuationFailure | None
) -> None:
    """Preserve the original success-or-failure lane after one-time claim."""

    if result is not None and failure is not None:
        raise ValueError("claimed continuation cannot contain both outcome lanes")


def _require_page(after: str | None, limit: int) -> None:
    """Validate bounded keyset pagination shared by every backend."""

    if after is not None:
        _require_text(after, "after")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("continuation list limit must be between 1 and 1000")


def _require_external_id(value: Any) -> None:
    """Bound private provider identifiers before persistence and indexing."""

    _require_text(value, "external_id")
    if len(value) > 1024:
        raise ValueError("external_id cannot exceed 1024 characters")


def _require_name(value: Any, name: str) -> None:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase stable identifier")


def _require_text(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "ContinuationConflictError",
    "ContinuationError",
    "ContinuationFailure",
    "ContinuationProvider",
    "ContinuationRecord",
    "ContinuationStatus",
    "ContinuationStore",
    "ContinuationValidationError",
    "ProviderPendingContinuation",
    "audit_continuation",
    "continuation_schema_id",
    "external_id_key",
]
