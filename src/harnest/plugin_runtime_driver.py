"""Runtime-driver ownership for same-process runtime plugins."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Sequence

from .plugin_runtime_manager import PluginRuntimeManager
from .runtime_contract import (
    AgentInfo,
    InvocationRequest,
    InvocationResult,
    RuntimeDriver,
    RuntimeEvent,
    SessionMessage,
    SessionRecord,
)


class PluginRuntimeDriver(RuntimeDriver):
    """Start plugins before delegated work and stop them after it drains."""

    def __init__(
        self, driver: RuntimeDriver, manager: PluginRuntimeManager
    ) -> None:
        """Retain one manager without acquiring authored resources eagerly."""

        if not isinstance(manager, PluginRuntimeManager):
            raise TypeError("manager must be PluginRuntimeManager")
        self._driver = driver
        self._manager = manager
        self._lock = asyncio.Lock()
        self._state = "new"

    @property
    def info(self) -> AgentInfo:
        return self._driver.info

    async def start(self) -> None:
        """Start plugins and then the inner extension/application boundary once."""

        if self._state == "started":
            return
        async with self._lock:
            if self._state == "started":
                return
            if self._state != "new":
                raise RuntimeError("plugin runtime driver cannot be restarted")
            try:
                await self._manager.start()
                await _start_driver(self._driver)
            except BaseException:
                self._state = "failed"
                raise
            self._state = "started"

    async def create_session(
        self,
        *,
        session_id: str,
        user_id: str,
        state: Mapping[str, Any],
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
        """Start plugins before forwarding one bounded session page."""

        await self.start()
        if after is None and limit is None:
            return await self._driver.list_sessions(user_id=user_id)
        return await self._driver.list_sessions(
            user_id=user_id, after=after, limit=limit
        )

    async def get_session_messages(
        self, *, session_id: str, user_id: str
    ) -> Sequence[SessionMessage] | None:
        """Start plugins before reading a framework-owned transcript."""

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
        """Drain the backend/extensions before releasing plugins exactly once."""

        async with self._lock:
            if self._state == "closed":
                return
            self._state = "closed"
        failure = await _cleanup_failure(self._driver.close)
        cleanup = await _cleanup_failure(self._manager.close)
        failure = _merge_failure(failure, cleanup)
        if failure is not None:
            raise failure


async def _start_driver(driver: RuntimeDriver) -> None:
    """Start only an explicit runtime wrapper, never an arbitrary backend hook."""

    starter = getattr(driver, "start", None)
    if callable(starter):
        await starter()


async def _cleanup_failure(
    callback: Callable[[], Awaitable[Any]],
) -> BaseException | None:
    """Detach cleanup sequencing from the first error's ownership."""

    try:
        await callback()
    except BaseException as error:
        return error
    return None


def _merge_failure(
    primary: BaseException | None, cleanup: BaseException | None
) -> BaseException | None:
    """Preserve backend priority while recording only a cleanup error type."""

    if cleanup is None:
        return primary
    if primary is None:
        return cleanup
    primary.add_note(
        f"plugin runtime cleanup also failed with {type(cleanup).__name__}"
    )
    return primary


__all__ = ["PluginRuntimeDriver"]
