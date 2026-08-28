"""Adapt Harnest's lifecycle-owned session store to Google ADK."""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
import contextvars
from datetime import datetime
import threading
from typing import Any, AsyncIterator, Mapping
from urllib.parse import urlparse
import uuid

from .neutral_runtime import SessionConflictError, SessionRecord
from .session import SessionLease, SessionStore


_EVENTS_KEY = "_harnest_adk_events"
_ACTIVE_LEASE: contextvars.ContextVar[tuple[str, str, SessionLease] | None] = (
    contextvars.ContextVar("harnest_adk_session_lease", default=None)
)
_SERVICE_SCHEME = "harnest-session-store"
_SERVICES: dict[str, Any] = {}
_SERVICES_LOCK = threading.Lock()


def create_adk_session_service(store: SessionStore) -> Any:
    """Return an ADK session service backed by one Harnest SessionStore."""

    try:
        from google.adk.sessions.base_session_service import BaseSessionService
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("Google ADK is required to adapt a SessionStore") from exc

    class HarnestADKSessionService(BaseSessionService):
        async def create_session(
            self,
            *,
            app_name: str,
            user_id: str,
            state: dict[str, Any] | None = None,
            session_id: str | None = None,
        ) -> Any:
            del app_name
            identifier = session_id or uuid.uuid4().hex
            initial = _packed_state(state or {}, ())
            try:
                record = await store.create(
                    session_id=identifier, user_id=user_id, state=initial
                )
            except SessionConflictError as exc:
                raise ValueError(f"session already exists: {identifier}") from exc
            return _adk_session(record)

        async def get_session(
            self,
            *,
            app_name: str,
            user_id: str,
            session_id: str,
            config: Any | None = None,
        ) -> Any | None:
            record = await _record(store, user_id=user_id, session_id=session_id)
            return None if record is None else _adk_session(record, app_name, config)

        async def list_sessions(
            self, *, app_name: str, user_id: str | None = None
        ) -> Any:
            if user_id is None:
                raise ValueError("Harnest session listing requires a user_id")
            from google.adk.sessions.base_session_service import ListSessionsResponse

            records = await store.list(user_id=user_id)
            sessions = [_adk_session(item, app_name) for item in records]
            sessions.sort(key=lambda item: item.last_update_time)
            return ListSessionsResponse(sessions=sessions)

        async def delete_session(
            self, *, app_name: str, user_id: str, session_id: str
        ) -> None:
            del app_name
            await store.delete(session_id=session_id, user_id=user_id)

        async def append_event(self, session: Any, event: Any) -> Any:
            persisted = await super().append_event(session, event)
            if getattr(event, "partial", False):
                return persisted
            state = _packed_state(session.state, session.events)
            lease = _active_lease(session.user_id, session.id)
            if lease is not None:
                await lease.replace_state(state)
                return persisted
            async with store.acquire(
                session_id=session.id, user_id=session.user_id
            ) as acquired:
                await acquired.replace_state(state)
            return persisted

        async def flush(self) -> None:
            return None

        @asynccontextmanager
        async def execution_lease(
            self, *, user_id: str, session_id: str
        ) -> AsyncIterator[None]:
            async with store.acquire(
                session_id=session_id, user_id=user_id
            ) as lease:
                token = _ACTIVE_LEASE.set((user_id, session_id, lease))
                try:
                    yield
                finally:
                    _ACTIVE_LEASE.reset(token)

    return HarnestADKSessionService()


@contextmanager
def register_adk_session_service(service: Any) -> Any:
    """Expose one service through ADK's public URI registry during app creation."""

    from google.adk.cli.utils.service_factory import get_service_registry

    identifier = uuid.uuid4().hex
    with _SERVICES_LOCK:
        _SERVICES[identifier] = service
        get_service_registry().register_session_service(
            _SERVICE_SCHEME, _registered_service
        )
    try:
        yield f"{_SERVICE_SCHEME}://{identifier}"
    finally:
        with _SERVICES_LOCK:
            _SERVICES.pop(identifier, None)


def _registered_service(uri: str, **_kwargs: Any) -> Any:
    identifier = urlparse(uri).netloc
    with _SERVICES_LOCK:
        service = _SERVICES.get(identifier)
    if service is None:
        raise RuntimeError("registered Harnest session service is unavailable")
    return service


async def _record(
    store: SessionStore, *, user_id: str, session_id: str
) -> SessionRecord | None:
    active = _active_lease(user_id, session_id)
    if active is not None:
        return active.record
    return await store.get(session_id=session_id, user_id=user_id)


def _active_lease(user_id: str, session_id: str) -> SessionLease | None:
    active = _ACTIVE_LEASE.get()
    if active is None or active[:2] != (user_id, session_id):
        return None
    return active[2]


def _packed_state(
    state: Mapping[str, Any], events: Any
) -> dict[str, Any]:
    if _EVENTS_KEY in state:
        raise ValueError(f"session state key {_EVENTS_KEY!r} is reserved by Harnest")
    result = dict(state)
    result[_EVENTS_KEY] = [_event_value(item) for item in events]
    return result


def _adk_session(
    record: SessionRecord, app_name: str = "harnest", config: Any | None = None
) -> Any:
    from google.adk.events import Event
    from google.adk.sessions import Session

    stored = dict(record.state)
    raw_events = stored.pop(_EVENTS_KEY, ())
    events = [Event.model_validate(item) for item in raw_events]
    events = _filtered_events(events, config)
    return Session(
        id=record.id,
        app_name=app_name,
        user_id=record.user_id,
        state=stored,
        events=events,
        last_update_time=_timestamp(record.updated_at),
    )


def _filtered_events(events: list[Any], config: Any | None) -> list[Any]:
    if config is None:
        return events
    after = getattr(config, "after_timestamp", None)
    if after is not None:
        events = [item for item in events if item.timestamp >= after]
    recent = getattr(config, "num_recent_events", None)
    return events[-recent:] if isinstance(recent, int) and recent >= 0 else events


def _event_value(event: Any) -> Mapping[str, Any]:
    dump = getattr(event, "model_dump", None)
    if not callable(dump):
        raise TypeError("ADK session events must be serializable models")
    return dump(mode="json", by_alias=True)


def _timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


__all__ = ["create_adk_session_service", "register_adk_session_service"]
