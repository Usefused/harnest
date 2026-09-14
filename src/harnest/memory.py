"""Explicit, user-scoped long-term memory without automatic model calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .memory_local import InMemoryStore


class MemoryConflictError(RuntimeError):
    """A conditional write refers to a stale or missing memory revision."""


class MemoryStorageError(RuntimeError):
    """Memory persistence failed without exposing private provider details."""


@dataclass(frozen=True, slots=True)
class MemoryScope:
    """Trusted application/user isolation, with an optional logical namespace."""

    application_id: str
    user_id: str
    namespace: str = "default"


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """One deliberate memory; timestamps are UTC epoch seconds.

    Providers assign opaque revisions and timestamps on writes. Source records
    provenance, not instructions or authorization. Payloads must be JSON-safe.
    """

    scope: MemoryScope
    key: str
    content: str = field(repr=False)
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False)
    source: Mapping[str, str] = field(default_factory=dict, repr=False)
    revision: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: float | None = None


@dataclass(frozen=True, slots=True)
class MemoryPage:
    """A bounded page; pass next_cursor as after, even for an empty page."""

    items: tuple[MemoryRecord, ...]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryCleanupPage:
    """Physical removals in a bounded sweep; continue even when deleted is zero."""

    deleted: int
    next_cursor: str | None = None


@runtime_checkable
class MemoryStore(Protocol):
    """Async persistence contract for explicit cross-session memories.

    Every operation must enforce the complete scope in the datastore. Expired
    records are invisible. Writes and revision checks are atomic. Reads return
    detached records. No method may invoke models or extract memories implicitly.
    """

    async def start(self) -> None:
        """Open owned resources and initialize or validate storage."""
        ...

    async def close(self) -> None:
        """Release only resources owned by this provider."""
        ...

    async def put(self, record: MemoryRecord, *, expected_revision: str | None = None) -> MemoryRecord:
        """Upsert atomically, preserving creation time and assigning a new revision.

        None allows an unconditional write. A supplied revision must match a
        live record, otherwise raise MemoryConflictError, including after deletion.
        """
        ...

    async def get(self, scope: MemoryScope, key: str) -> MemoryRecord | None:
        """Read one live record by its fully scoped key."""
        ...

    async def list(self, scope: MemoryScope, *, limit: int = 20, after: str | None = None) -> MemoryPage:
        """Page live records in ascending key order; limit is 1 through 100."""
        ...

    async def search(self, scope: MemoryScope, query: str, *, limit: int = 20, after: str | None = None) -> MemoryPage:
        """Page literal, case-sensitive content matches, not semantic similarity.

        Apply scope, query, expiry, ordering and limits in the datastore. Bounded
        scans may yield empty pages with a continuation cursor. Cursors are not
        snapshots: concurrent edits may change subsequent results.
        """
        ...

    async def delete(self, scope: MemoryScope, key: str, *, expected_revision: str | None = None) -> bool:
        """Atomically remove the record and index references; return false if absent.

        A supplied stale revision raises MemoryConflictError. Deletion cannot
        erase copies already returned to callers or retained in database backups.
        """
        ...

    async def delete_all(self, scope: MemoryScope) -> int:
        """Physically erase a namespace, including expired records; return count.

        Trusted application maintenance only: no HTTP route or model tool is
        registered. Revoke writes first when using this for erasure, because
        concurrent new writes may survive. Backups and returned copies remain.
        """
        ...

    async def delete_user(self, application_id: str, user_id: str) -> int:
        """Physically erase all namespaces for one application's user.

        Trusted application code must authorize the owner and revoke their
        writes first. Cleanup may be batched, not one global transaction;
        on failure retry to remove remaining records. Return the removed count.
        """
        ...

    async def purge_expired(self, scope: MemoryScope, *, limit: int = 100, after: str | None = None) -> MemoryCleanupPage:
        """Physically sweep at most limit keys in ascending order for expiry.

        Continue while a cursor is present, even when deleted is zero. Repeat
        sweeps for records that expire after the cursor passes. This maintenance
        operation is provider-only, not automatically scheduled or model-facing.
        """
        ...


def __getattr__(name: str) -> Any:
    """Load the reference provider lazily to avoid contract/provider cycles."""
    if name == "InMemoryStore":
        from .memory_local import InMemoryStore
        return InMemoryStore
    raise AttributeError(name)

__all__ = ["MemoryScope", "MemoryRecord", "MemoryPage", "MemoryCleanupPage", "MemoryStore", "MemoryConflictError", "MemoryStorageError", "InMemoryStore"]
