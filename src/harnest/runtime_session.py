"""Runtime ownership for lifecycle-created storage resources."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Mapping, Sequence

from .runtime_contract import (
    AgentInfo,
    InvocationRequest,
    InvocationResult,
    RuntimeDriver,
    RuntimeEvent,
    SessionMessage,
    SessionRecord,
)
from .session import SessionStore
from .storage_registry import StorageRegistry


class StorageRuntimeDriver(RuntimeDriver):
    """Start and close session/checkpoint resources around a runtime driver."""

    def __init__(
        self,
        driver: RuntimeDriver,
        store: SessionStore | None = None,
        checkpoint_provider: Any | None = None,
        asset_store: Any | None = None,
        *asset_stores: Any,
        storage_registry: StorageRegistry | None = None,
    ) -> None:
        """Deduplicate shared resources so each is started and closed once."""

        self._driver = driver
        legacy = (store, checkpoint_provider, asset_store, *asset_stores)
        if storage_registry is not None and any(item is not None for item in legacy):
            raise ValueError("storage_registry cannot be combined with legacy resources")
        self._resources = (
            storage_registry.owned_resources()
            if storage_registry is not None
            else _unique_resources(*legacy)
        )
        self._start_lock = asyncio.Lock()
        self._started = False
        self._start_failed = False
        self._closed = False
        self._entered_resources: list[Any] = []

    @property
    def info(self) -> AgentInfo:
        return self._driver.info

    async def _start(self) -> None:
        """Start shared storage once before either session or invocation traffic."""

        self._require_startable()
        async with self._start_lock:
            self._require_startable()
            if self._started:
                return
            try:
                for resource in self._resources:
                    # A start hook may acquire a connection before it raises.
                    # Enter ownership before awaiting so partial initialization
                    # receives exactly one matching close attempt.
                    self._entered_resources.append(resource)
                    await resource.start()
            except BaseException as failure:
                self._start_failed = True
                resources = self._take_entered_resources()
                cleanup = await _close_resources(resources)
                _merge_cleanup_failure(
                    failure, cleanup, label="storage startup"
                )
                raise
            self._started = True

    def _require_startable(self) -> None:
        """Reject reuse after close or a transactionally unwound startup."""

        if self._closed:
            raise RuntimeError("storage runtime is closed")
        if self._start_failed:
            raise RuntimeError("storage runtime failed to start")

    def _take_entered_resources(self) -> tuple[Any, ...]:
        """Transfer cleanup ownership so no resource can be closed twice."""

        resources = tuple(self._entered_resources)
        self._entered_resources.clear()
        return resources

    async def start_owned_resources(self) -> None:
        """Let an outer lifecycle wrapper establish storage before its hooks run."""

        await self._start()

    async def start(self) -> None:
        """Start storage first, then eagerly enter the wrapped runtime stages."""

        await self._start()
        starter = getattr(self._driver, "start", None)
        if callable(starter):
            await starter()

    async def create_session(
        self, *, session_id: str, user_id: str, state: Mapping[str, Any]
    ) -> SessionRecord:
        await self.start()
        return await self._driver.create_session(
            session_id=session_id, user_id=user_id, state=state
        )

    async def get_session(
        self, *, session_id: str, user_id: str
    ) -> SessionRecord | None:
        await self.start()
        return await self._driver.get_session(
            session_id=session_id, user_id=user_id
        )

    async def list_sessions(
        self,
        *,
        user_id: str,
        after: str | None = None,
        limit: int | None = None,
    ) -> Sequence[SessionRecord]:
        """Start storage before forwarding optional session keyset bounds."""

        await self.start()
        if after is None and limit is None:
            return await self._driver.list_sessions(user_id=user_id)
        return await self._driver.list_sessions(
            user_id=user_id, after=after, limit=limit
        )

    async def get_session_messages(
        self, *, session_id: str, user_id: str
    ) -> Sequence[SessionMessage] | None:
        """Start storage before delegating transcript retrieval."""

        await self.start()
        return await self._driver.get_session_messages(
            session_id=session_id, user_id=user_id
        )

    async def update_session(
        self,
        *,
        session_id: str,
        user_id: str,
        state_delta: Mapping[str, Any],
    ) -> SessionRecord | None:
        await self.start()
        return await self._driver.update_session(
            session_id=session_id,
            user_id=user_id,
            state_delta=state_delta,
        )

    async def delete_session(self, *, session_id: str, user_id: str) -> bool:
        await self.start()
        return await self._driver.delete_session(
            session_id=session_id, user_id=user_id
        )

    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        await self.start()
        return await self._driver.invoke(request)

    async def stream(
        self, request: InvocationRequest
    ) -> AsyncIterator[RuntimeEvent]:
        await self.start()
        async for event in self._driver.stream(request):
            yield event

    async def close(self) -> None:
        """Close framework work first, then unwind owned storage in reverse order."""

        async with self._start_lock:
            if self._closed:
                return
            self._closed = True
            resources = self._take_entered_resources()
        failure: BaseException | None = None
        try:
            await self._driver.close()
        except BaseException as exc:
            failure = exc
        cleanup = await _close_resources(resources)
        failure = _merge_cleanup_failure(
            failure, cleanup, label="storage"
        )
        if failure is not None:
            raise failure


async def _close_resources(resources: Sequence[Any]) -> BaseException | None:
    """Close entered resources in reverse order and attempt every cleanup."""

    failure: BaseException | None = None
    for resource in reversed(resources):
        try:
            await resource.close()
        except BaseException as error:
            failure = _merge_cleanup_failure(
                failure, error, label="storage"
            )
    return failure


def _merge_cleanup_failure(
    primary: BaseException | None,
    cleanup: BaseException | None,
    *,
    label: str,
) -> BaseException | None:
    """Preserve primary failure priority and record only cleanup error type."""

    if cleanup is None:
        return primary
    if primary is None:
        return cleanup
    primary.add_note(f"{label} cleanup also failed with {type(cleanup).__name__}")
    return primary


def _unique_resources(*values: Any) -> tuple[Any, ...]:
    """Preserve ownership order while removing identical shared stores."""

    result: list[Any] = []
    for value in values:
        if value is not None and all(value is not item for item in result):
            result.append(value)
    return tuple(result)


__all__ = ["StorageRuntimeDriver"]
