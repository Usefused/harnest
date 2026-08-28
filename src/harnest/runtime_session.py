"""Runtime ownership for lifecycle-created storage resources."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Mapping, Sequence

from .neutral_runtime import (
    AgentInfo,
    InvocationRequest,
    InvocationResult,
    RuntimeDriver,
    RuntimeEvent,
    SessionMessage,
    SessionRecord,
)
from .session import SessionStore


class StorageRuntimeDriver(RuntimeDriver):
    """Start and close session/checkpoint resources around a runtime driver."""

    def __init__(
        self,
        driver: RuntimeDriver,
        store: SessionStore | None = None,
        checkpoint_provider: Any | None = None,
    ) -> None:
        """Deduplicate shared resources so each is started and closed once."""

        self._driver = driver
        self._store = store
        self._resources = _unique_resources(store, checkpoint_provider)
        self._start_lock = asyncio.Lock()
        self._started = False
        self._closed = False

    @property
    def info(self) -> AgentInfo:
        return self._driver.info

    async def _start(self) -> None:
        """Start shared storage once before either session or invocation traffic."""

        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            if self._closed:
                raise RuntimeError("storage runtime is closed")
            for resource in self._resources:
                await resource.start()
            self._started = True

    async def create_session(
        self, *, session_id: str, user_id: str, state: Mapping[str, Any]
    ) -> SessionRecord:
        await self._start()
        return await self._driver.create_session(
            session_id=session_id, user_id=user_id, state=state
        )

    async def get_session(
        self, *, session_id: str, user_id: str
    ) -> SessionRecord | None:
        await self._start()
        return await self._driver.get_session(
            session_id=session_id, user_id=user_id
        )

    async def list_sessions(self, *, user_id: str) -> Sequence[SessionRecord]:
        await self._start()
        return await self._driver.list_sessions(user_id=user_id)

    async def get_session_messages(
        self, *, session_id: str, user_id: str
    ) -> Sequence[SessionMessage] | None:
        """Start storage before delegating transcript retrieval."""

        await self._start()
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
        await self._start()
        return await self._driver.update_session(
            session_id=session_id,
            user_id=user_id,
            state_delta=state_delta,
        )

    async def delete_session(self, *, session_id: str, user_id: str) -> bool:
        await self._start()
        return await self._driver.delete_session(
            session_id=session_id, user_id=user_id
        )

    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        await self._start()
        return await self._driver.invoke(request)

    async def stream(
        self, request: InvocationRequest
    ) -> AsyncIterator[RuntimeEvent]:
        await self._start()
        async for event in self._driver.stream(request):
            yield event

    async def close(self) -> None:
        """Close framework work first, then unwind owned storage in reverse order."""

        if self._closed:
            return
        self._closed = True
        failure: BaseException | None = None
        try:
            await self._driver.close()
        except BaseException as exc:
            failure = exc
        for resource in reversed(self._resources):
            try:
                await resource.close()
            except BaseException as exc:
                if failure is None:
                    failure = exc
                else:
                    failure.add_note(
                        f"storage cleanup also failed with {type(exc).__name__}"
                    )
        if failure is not None:
            raise failure


def _unique_resources(*values: Any) -> tuple[Any, ...]:
    """Preserve ownership order while removing identical shared stores."""

    result: list[Any] = []
    for value in values:
        if value is not None and all(value is not item for item in result):
            result.append(value)
    return tuple(result)


__all__ = ["StorageRuntimeDriver"]
