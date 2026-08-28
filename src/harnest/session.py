"""Framework-neutral lifecycle session storage contracts."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncContextManager, AsyncIterator, Mapping, Protocol, Sequence, runtime_checkable

from ._json import json_value
from .runtime_contract import SessionConflictError, SessionRecord


@dataclass(frozen=True, slots=True)
class ADKSessionStorage:
    """Deployment-owned ADK session backend configuration."""

    uri: str
    database_kwargs: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.uri, str) or not self.uri.strip():
            raise ValueError("ADK session storage uri must be non-empty")

    def create_service(self, base_directory: str) -> Any:
        from google.adk.cli.utils.service_factory import (
            create_session_service_from_options,
        )

        return create_session_service_from_options(
            base_dir=base_directory,
            session_service_uri=self.uri,
            session_db_kwargs=dict(self.database_kwargs),
            use_local_storage=False,
        )


@runtime_checkable
class SessionLease(Protocol):
    """Exclusive session access held across one graph execution."""

    @property
    def record(self) -> SessionRecord: ...

    async def patch_state(self, delta: Mapping[str, Any]) -> None: ...

    async def replace_state(self, state: Mapping[str, Any]) -> None: ...


@runtime_checkable
class SessionStore(Protocol):
    """Persistent session state with tenant scoping and execution leases.

    Production implementations should use set-based listing, durable writes,
    and a distributed lease. Durable mutations are also responsible for the
    repository's privacy-safe OTEL audit contract after commit.
    """

    async def start(self) -> None: ...

    async def create(
        self, *, session_id: str, user_id: str, state: Mapping[str, Any]
    ) -> SessionRecord: ...

    async def get(self, *, session_id: str, user_id: str) -> SessionRecord | None: ...

    async def list(
        self,
        *,
        user_id: str,
        after: str | None = None,
        limit: int | None = None,
    ) -> Sequence[SessionRecord]: ...

    async def update(
        self,
        *,
        session_id: str,
        user_id: str,
        state_delta: Mapping[str, Any],
    ) -> SessionRecord | None: ...

    async def delete(self, *, session_id: str, user_id: str) -> bool: ...

    def acquire(
        self, *, session_id: str, user_id: str
    ) -> AsyncContextManager[SessionLease]: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class _StoredSession:
    user_id: str
    state: dict[str, Any]
    created_at: str = field(default_factory=lambda: _timestamp())
    updated_at: str = field(default_factory=lambda: _timestamp())
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class _InMemoryLease:
    def __init__(self, session_id: str, stored: _StoredSession) -> None:
        self._session_id = session_id
        self._stored = stored

    @property
    def record(self) -> SessionRecord:
        return _record(self._session_id, self._stored, serialize=False)

    async def patch_state(self, delta: Mapping[str, Any]) -> None:
        self._stored.state.update(dict(delta))
        self._stored.updated_at = _timestamp()

    async def replace_state(self, state: Mapping[str, Any]) -> None:
        self._stored.state = dict(state)
        self._stored.updated_at = _timestamp()


class InMemorySessionStore:
    """Development store; inject a durable SessionStore in production."""

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], _StoredSession] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Satisfy the application-owned store lifecycle without external I/O."""

    async def create(
        self, *, session_id: str, user_id: str, state: Mapping[str, Any]
    ) -> SessionRecord:
        _require_text(session_id, "session_id")
        _require_text(user_id, "user_id")
        key = (user_id, session_id)
        async with self._lock:
            if key in self._sessions:
                raise SessionConflictError("session already exists")
            stored = _StoredSession(user_id=user_id, state=dict(state))
            self._sessions[key] = stored
        return _record(session_id, stored)

    async def get(
        self, *, session_id: str, user_id: str
    ) -> SessionRecord | None:
        stored = self._sessions.get((user_id, session_id))
        if stored is None:
            return None
        async with stored.lock:
            return _record(session_id, stored)

    async def list(
        self,
        *,
        user_id: str,
        after: str | None = None,
        limit: int | None = None,
    ) -> Sequence[SessionRecord]:
        """List an ordered keyset page without materializing unrelated users."""

        _require_text(user_id, "user_id")
        _require_list_options(after, limit)
        pairs = sorted(
            (
                (session_id, stored)
                for (owner, session_id), stored in self._sessions.items()
                if owner == user_id and (after is None or session_id > after)
            ),
            key=lambda item: item[0],
        )
        if limit is not None:
            pairs = pairs[:limit]
        return tuple(_record(session_id, stored) for session_id, stored in pairs)

    async def update(
        self,
        *,
        session_id: str,
        user_id: str,
        state_delta: Mapping[str, Any],
    ) -> SessionRecord | None:
        stored = self._sessions.get((user_id, session_id))
        if stored is None:
            return None
        async with stored.lock:
            stored.state.update(dict(state_delta))
            stored.updated_at = _timestamp()
            return _record(session_id, stored)

    async def delete(self, *, session_id: str, user_id: str) -> bool:
        key = (user_id, session_id)
        stored = self._sessions.get(key)
        if stored is None:
            return False
        # Deletion waits for an active execution lease so a completed graph
        # cannot write into a session that has already disappeared publicly.
        async with stored.lock:
            async with self._lock:
                if self._sessions.get(key) is not stored:
                    return False
                self._sessions.pop(key)
                return True

    @asynccontextmanager
    async def acquire(
        self, *, session_id: str, user_id: str
    ) -> AsyncIterator[SessionLease]:
        stored = self._sessions.get((user_id, session_id))
        if stored is None:
            raise KeyError("session not found")
        async with stored.lock:
            yield _InMemoryLease(session_id, stored)

    async def close(self) -> None:
        async with self._lock:
            self._sessions.clear()


def _record(
    session_id: str, stored: _StoredSession, *, serialize: bool = True
) -> SessionRecord:
    state = json_value(stored.state) if serialize else dict(stored.state)
    return SessionRecord(
        id=session_id,
        user_id=stored.user_id,
        state=state,
        created_at=stored.created_at,
        updated_at=stored.updated_at,
    )


def _require_text(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_list_options(after: str | None, limit: int | None) -> None:
    """Validate optional keyset bounds shared by session-store implementations."""

    if after is not None:
        _require_text(after, "after")
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("limit must be a positive integer")


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "ADKSessionStorage",
    "InMemorySessionStore",
    "SessionLease",
    "SessionStore",
]
