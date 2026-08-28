"""Runtime ownership for a lifecycle-created session store."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Mapping, Sequence

from .neutral_runtime import (
    AgentInfo,
    InvocationRequest,
    InvocationResult,
    RuntimeDriver,
    RuntimeEvent,
    SessionRecord,
)
from .session import SessionStore


class SessionStoreRuntimeDriver(RuntimeDriver):
    """Start and close one extension-created store around a runtime driver."""

    def __init__(self, driver: RuntimeDriver, store: SessionStore) -> None:
        self._driver = driver
        self._store = store
        self._start_lock = asyncio.Lock()
        self._started = False
        self._closed = False

    @property
    def info(self) -> AgentInfo:
        return self._driver.info

    async def _start(self) -> None:
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            if self._closed:
                raise RuntimeError("session store runtime is closed")
            await self._store.start()
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
        if self._closed:
            return
        self._closed = True
        failure: BaseException | None = None
        try:
            await self._driver.close()
        except BaseException as exc:
            failure = exc
        try:
            await self._store.close()
        except BaseException as exc:
            if failure is None:
                failure = exc
            else:
                failure.add_note(
                    f"session store cleanup also failed with {type(exc).__name__}"
                )
        if failure is not None:
            raise failure


__all__ = ["SessionStoreRuntimeDriver"]
