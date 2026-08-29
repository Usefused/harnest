"""Invocation-scoped application data kept outside framework session state."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from ._json import json_value
from .context import ContextUnavailableError, context
from .logging import get_logger
from .session import SessionLease
from .session import SessionStore


_AUDIT = get_logger("session.audit")
_MISSING = object()


class SessionDataError(RuntimeError):
    """Raised when application session data violates its scoped contract."""


@dataclass(slots=True)
class _SessionLifetime:
    """Revoke facades copied into child tasks when the invocation completes."""

    active: bool = True


@dataclass(frozen=True, slots=True)
class _SessionBinding:
    lease: SessionLease = field(repr=False)
    store: SessionStore | None = field(repr=False, compare=False)
    framework: str
    invocation_id: str | None
    trigger: str
    lifetime: _SessionLifetime = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class SessionContext:
    """Provide JSON-safe access to the current session's application-data lane."""

    _binding: _SessionBinding = field(repr=False)
    _path: tuple[str, ...] = field(default=(), repr=False)

    async def get(self, key: str, default: Any = None) -> Any:
        """Return a detached value without exposing mutable store-owned state."""

        _require_key(key)
        container = self._container(create=False)
        if container is None or key not in container:
            return json_value(default)
        return json_value(container[key])

    async def set(self, key: str, value: Any) -> None:
        """Persist one value without projecting it into framework model state."""

        _require_key(key)
        normalized = json_value(value)
        document = self._document()
        self._container(document=document, create=True)[key] = normalized
        await self._commit(document, "set")

    async def update(self, values: Mapping[str, Any]) -> None:
        """Persist a bounded mapping in one lease-owned write."""

        normalized = _application_mapping(values)
        if not normalized:
            return
        document = self._document()
        self._container(document=document, create=True).update(normalized)
        await self._commit(document, "update")

    async def delete(self, key: str) -> bool:
        """Delete one value and report whether the current namespace contained it."""

        _require_key(key)
        document = self._document()
        container = self._container(document=document, create=False)
        if container is None or key not in container:
            return False
        container.pop(key)
        await self._commit(document, "delete")
        return True

    def namespace(self, name: str) -> "SessionContext":
        """Return a nested view so teams and plugins can avoid key collisions."""

        _require_key(name)
        self._require_active()
        return SessionContext(self._binding, (*self._path, name))

    def _document(self) -> dict[str, Any]:
        """Copy the latest leased document before applying an isolated mutation."""

        self._require_active()
        value = json_value(self._binding.lease.record.application_data)
        if not isinstance(value, dict):  # pragma: no cover - stores own this invariant
            raise SessionDataError("session application data must be an object")
        return value

    def _container(
        self,
        *,
        document: dict[str, Any] | None = None,
        create: bool,
    ) -> dict[str, Any] | None:
        """Resolve a namespace while rejecting scalar/container collisions."""

        current = self._document() if document is None else document
        for part in self._path:
            value = current.get(part, _MISSING)
            if value is _MISSING and create:
                value = {}
                current[part] = value
            if value is _MISSING:
                return None
            if not isinstance(value, dict):
                raise SessionDataError(
                    "session namespace conflicts with an existing non-object value"
                )
            current = value
        return current

    async def _commit(self, document: Mapping[str, Any], operation: str) -> None:
        """Audit the same trace after commit or when its attempted write fails."""

        self._require_active()
        try:
            await self._binding.lease.replace_application_data(document)
        except Exception:
            self._audit(operation, "failed")
            raise
        self._audit(operation, "committed")

    def _audit(self, operation: str, outcome: str) -> None:
        """Emit only low-cardinality mutation facts; OTEL carries correlation."""

        event = f"session.application_data.{operation}"
        _AUDIT.info(
            event,
            operation=event,
            trigger=self._binding.trigger,
            outcome=outcome,
            framework=self._binding.framework,
        )

    def _require_active(self) -> None:
        if not self._binding.lifetime.active:
            raise ContextUnavailableError(
                "session context is available only during a managed invocation"
            )


_ACTIVE_SESSION: ContextVar[_SessionBinding | None] = ContextVar(
    "harnest_session_context", default=None
)


class _SessionAccess:
    """Resolve the application-data facade bound to the active invocation."""

    def current(self) -> SessionContext:
        """Return a scope-checked facade without revealing the underlying lease."""

        active = context.current()
        binding = _ACTIVE_SESSION.get()
        if binding is None:
            raise ContextUnavailableError(
                "session context is unavailable at this lifecycle stage"
            )
        record = binding.lease.record
        same_scope = (record.user_id, record.id) == (
            active.user_id,
            active.session_id,
        )
        same_invocation = binding.invocation_id in {None, active.invocation_id}
        if not same_scope or not same_invocation:
            raise ContextUnavailableError(
                "session context does not belong to the active invocation"
            )
        return SessionContext(binding)

    async def get(self, key: str, default: Any = None) -> Any:
        return await self.current().get(key, default)

    async def set(self, key: str, value: Any) -> None:
        await self.current().set(key, value)

    async def update(self, values: Mapping[str, Any]) -> None:
        await self.current().update(values)

    async def delete(self, key: str) -> bool:
        return await self.current().delete(key)

    def namespace(self, name: str) -> SessionContext:
        return self.current().namespace(name)


session = _SessionAccess()


def current_session_lease(
    *,
    store: SessionStore | None = None,
    user_id: str,
    session_id: str,
    invocation_id: str | None,
) -> SessionLease | None:
    """Return the matching active lease without exposing it to agent code."""

    binding = _ACTIVE_SESSION.get()
    if binding is None or not binding.lifetime.active:
        return None
    if store is not None and binding.store is not store:
        return None
    record = binding.lease.record
    same_scope = (record.user_id, record.id) == (user_id, session_id)
    same_invocation = binding.invocation_id in {None, invocation_id}
    return binding.lease if same_scope and same_invocation else None


@asynccontextmanager
async def invocation_session_context(
    store: SessionStore | None,
    *,
    framework: str,
    user_id: str,
    session_id: str,
    invocation_id: str,
    trigger: str = "agent",
) -> AsyncIterator[SessionLease | None]:
    """Acquire or reuse the invocation lease across every lifecycle stage."""

    if store is None:
        yield None
        return
    active = current_session_lease(
        store=store,
        user_id=user_id,
        session_id=session_id,
        invocation_id=invocation_id,
    )
    if active is not None:
        # Framework adapters call this helper inside the outer agent lifecycle.
        # Reusing authority avoids deadlocking non-reentrant distributed leases.
        yield active
        return
    async with store.acquire(session_id=session_id, user_id=user_id) as lease:
        with activate_session_context(
            lease,
            store=store,
            framework=framework,
            invocation_id=invocation_id,
            trigger=trigger,
        ):
            yield lease


@contextmanager
def activate_session_context(
    lease: SessionLease,
    *,
    store: SessionStore | None = None,
    framework: str,
    invocation_id: str | None,
    trigger: str = "agent",
) -> Iterator[None]:
    """Bind one already-held lease so session access cannot reacquire and deadlock."""

    if trigger not in {"agent", "user"}:
        raise ValueError("session mutation trigger must be 'agent' or 'user'")
    lifetime = _SessionLifetime()
    binding = _SessionBinding(
        lease=lease,
        store=store,
        framework=framework,
        invocation_id=invocation_id,
        trigger=trigger,
        lifetime=lifetime,
    )
    token = _ACTIVE_SESSION.set(binding)
    try:
        yield
    finally:
        # Context variables are copied into child tasks; shared revocation closes
        # the capability even when a copied binding outlives its invocation.
        lifetime.active = False
        _ACTIVE_SESSION.reset(token)


def _application_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(values, Mapping):
        raise TypeError("session update values must be a mapping")
    for key in values:
        _require_key(key)
    normalized = json_value(values)
    if not isinstance(normalized, dict):  # pragma: no cover - mapping normalized above
        raise TypeError("session update values must normalize to an object")
    return normalized


def _require_key(value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("session data keys and namespaces must be non-empty strings")


__all__ = [
    "SessionContext",
    "SessionDataError",
    "activate_session_context",
    "current_session_lease",
    "invocation_session_context",
    "session",
]
