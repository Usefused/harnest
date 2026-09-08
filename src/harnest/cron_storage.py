"""Public durable schedule contract with atomic task occurrence commits."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping, Protocol, runtime_checkable

from .task_storage import TaskRecord, plain_json


class CronStoreConflictError(RuntimeError):
    """A schedule definition, terminal state or expected revision conflicts."""


@dataclass(frozen=True, slots=True)
class CronRecord:
    """Private user-owned schedule with a durable next-occurrence cursor."""

    schedule_id: str
    application_id: str
    user_id: str
    key: str
    expression: str
    task_name: str
    arguments: Mapping[str, Any] = field(repr=False)
    next_run_at: float
    status: str = "active"
    revision: int = 0
    timezone: str = "UTC"
    created_at: float = 0.0
    updated_at: float = 0.0


@runtime_checkable
class CronStore(Protocol):
    """Durable CRUD and occurrence handoff on the same provider as TaskStore.

    User calls always include application and user predicates. Ordered queries
    and pagination run in the datastore. Cron parsing and next-run calculation
    belong to Harnest core. Providers enforce revisions and atomic handoff.
    """

    async def start(self) -> None:
        """Open provider resources; a shared TaskStore instance starts once."""
        ...

    async def close(self) -> None:
        """Release provider resources after the owning runtime stops dispatch."""
        ...

    async def create_cron(self, record: CronRecord) -> CronRecord:
        """Create, or return exact definition at application/user/key identity.

        Exact retries return existing paused/cancelled schedules unchanged.
        Different definitions and conflicting schedule IDs raise conflict.
        """
        ...

    async def get_cron(
        self, *, application_id: str, user_id: str, schedule_id: str
    ) -> CronRecord | None:
        """Read one owner-scoped schedule without disclosing other owners."""
        ...

    async def list_crons(
        self, *, application_id: str, user_id: str,
        after: str | None = None, limit: int = 100,
    ) -> tuple[CronRecord, ...]:
        """Return schedules after the ID cursor; limit must be from 1 to 1000."""
        ...

    async def update_cron(
        self, record: CronRecord, *, expected_revision: int
    ) -> CronRecord:
        """Replace mutable fields and increment revision with an atomic CAS.

        IDs, owner, key and task name are immutable. Cancelled is terminal.
        An identical cancelled retry returns the existing record unchanged.
        Missing records raise KeyError; state/revision conflicts raise conflict.
        """
        ...

    async def delete_cron(
        self, *, application_id: str, user_id: str, schedule_id: str
    ) -> bool:
        """Remove only an owner-scoped schedule; missing/foreign returns false."""
        ...

    async def list_due_crons(
        self, *, application_id: str, now: float,
        after: tuple[float, str] | None = None, limit: int = 100,
    ) -> tuple[CronRecord, ...]:
        """Page active due schedules by time then ID; limit is from 1 to 1000."""
        ...

    async def commit_cron_occurrence(
        self, *, application_id: str, user_id: str, schedule_id: str,
        expected_revision: int, due_at: float, next_run_at: float,
        task: TaskRecord,
    ) -> TaskRecord | None:
        """Atomically enqueue one occurrence and advance the active schedule.

        Require matching owner, revision and next_run_at==due_at, and a task
        matching the schedule owner/target/arguments. next_run_at must advance. Stale or
        inactive returns None with no task insert. Duplicate deliveries cannot
        create another task. Advance revision within the same transaction.
        """
        ...


def cron_fingerprint(record: CronRecord) -> str:
    """Compare authored definitions without transient schedule lifecycle state."""

    encoded = json.dumps(
        plain_json({"expression": record.expression, "task_name": record.task_name,
                    "arguments": record.arguments, "timezone": record.timezone}),
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = ["CronRecord", "CronStore", "CronStoreConflictError"]
