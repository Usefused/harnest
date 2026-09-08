"""Public atomic persistence contract for Harnest's durable task workers."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping, Protocol, runtime_checkable


class TaskStoreConflictError(RuntimeError):
    """A stable job identity or idempotency key has a different definition."""


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """Private persisted job, including reconstructable invocation capabilities.

    Epoch timestamps are UTC seconds. Attempts count claims, starting at one;
    ``max_retries`` permits that many attempts after the first. Providers retain
    an immutable enqueue fingerprint after clearing terminal private payloads.
    """

    job_id: str
    application_id: str
    user_id: str
    task_name: str
    queue: str
    arguments: Mapping[str, Any] = field(repr=False)
    invocation: Mapping[str, Any] | None = field(default=None, repr=False)
    agent_permissions: tuple[str, ...] | None = field(default=None, repr=False)
    trigger: str = "agent"
    status: str = "pending"
    scheduled_at: float = 0.0
    max_retries: int = 3
    attempt: int = 0
    lease_token: str | None = field(default=None, repr=False)
    lease_expires_at: float | None = None
    result: Any = field(default=None, repr=False)
    failure_code: str | None = None
    idempotency_key: str | None = field(default=None, repr=False)
    created_at: float = 0.0
    updated_at: float = 0.0


@runtime_checkable
class TaskStore(Protocol):
    """Atomic operations required for at-least-once task execution.

    All selection, scope predicates, ordering, and limits execute inside the
    datastore. A provider must durably commit before returning success. Expired
    lease holders cannot renew or finish work. Terminal transitions clear
    arguments, invocation and permissions while retaining the result/failure.
    """

    async def start(self) -> None:
        """Open owned resources and initialize the provider's durable schema."""
        ...

    async def close(self) -> None:
        """Release owned resources after workers and dispatchers have stopped."""
        ...

    async def enqueue_task(self, record: TaskRecord) -> TaskRecord:
        """Atomically persist payload and job, or return an identical submission.

        Idempotency is scoped by application, user, task name and optional key.
        Conflicting definitions raise TaskStoreConflictError. Persist a separate
        fingerprint so terminal payload cleanup cannot defeat deduplication.
        """
        ...

    async def get_task(
        self, *, application_id: str, job_id: str, user_id: str | None = None
    ) -> TaskRecord | None:
        """Read one scoped job; omitted user is a trusted worker-only lookup."""
        ...

    async def claim_tasks(
        self, *, application_id: str, queues: tuple[str, ...], now: float,
        lease_seconds: float, limit: int = 1,
    ) -> tuple[TaskRecord, ...]:
        """Atomically claim due pending or expired running jobs in queue scope.

        Return a bounded eligible batch with best-effort earliest-due ordering;
        concurrent workers and lease recovery do not guarantee strict FIFO.
        Each claim increments attempt and issues a fresh lease token.
        Expired final attempts become failed with
        task_failed instead of remaining stranded or retrying indefinitely.
        Empty queues selects no work. Limits are integers from 1 to 1000 and
        apply to returned claims.
        """
        ...

    async def renew_task_lease(
        self, *, application_id: str, job_id: str, lease_token: str,
        now: float, lease_seconds: float,
    ) -> bool:
        """Extend a running, unexpired matching lease; stale owners return false."""
        ...

    async def finish_task(
        self, *, application_id: str, job_id: str, lease_token: str,
        now: float, status: str, result: Any = None,
        failure_code: str | None = None, retry_at: float | None = None,
    ) -> bool:
        """Fence a completion, failure or retry with a matching unexpired lease.

        Status is completed, failed, or pending. Pending requires retry_at and
        remaining retries; it preserves payload. All transitions clear leases.
        Stale owners return false and must never overwrite another attempt.
        """
        ...

    async def cancel_task(
        self, *, application_id: str, user_id: str, job_id: str, now: float
    ) -> bool:
        """Atomically cancel pending/running work, invalidate leases and scrub.

        Unknown, foreign and terminal jobs return false. A running effect may
        already have happened; cancellation prevents subsequent state commits.
        """
        ...


def task_fingerprint(record: TaskRecord) -> str:
    """Keep enqueue equivalence stable across retries and terminal scrubbing."""

    value = {
        "task_name": record.task_name,
        "queue": record.queue,
        "arguments": dict(record.arguments),
        "agent_permissions": record.agent_permissions,
        "max_retries": record.max_retries,
    }
    encoded = json.dumps(plain_json(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def plain_json(value: Any) -> Any:
    """Normalize immutable runtime containers before a provider serializes JSON."""

    if isinstance(value, Mapping):
        return {key: plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain_json(item) for item in value]
    return value


__all__ = ["TaskRecord", "TaskStore", "TaskStoreConflictError"]
