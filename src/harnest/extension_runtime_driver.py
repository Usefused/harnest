"""Runtime-driver ownership for same-process Harnest Extensions."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Sequence

from ._exception_notes import add_exception_note
from .extension_runtime_manager import ExtensionRuntimeManager
from .runtime_contract import (
    AgentInfo,
    InvocationRequest,
    InvocationResult,
    RuntimeDriver,
    RuntimeEvent,
    SessionMessage,
    SessionRecord,
)


class ExtensionHostRuntimeDriver(RuntimeDriver):
    """Start extensions before delegated work and stop them after it drains."""

    def __init__(
        self, driver: RuntimeDriver, manager: ExtensionRuntimeManager
    ) -> None:
        """Retain one manager without acquiring authored resources eagerly."""

        if not isinstance(manager, ExtensionRuntimeManager):
            raise TypeError("manager must be ExtensionRuntimeManager")
        self._driver = driver
        self._manager = manager
        self._lock = asyncio.Lock()
        self._state = "new"

    @property
    def info(self) -> AgentInfo:
        return self._driver.info

    async def start(self) -> None:
        """Start extensions and then the inner application boundary once."""

        if self._state == "started":
            return
        async with self._lock:
            if self._state == "started":
                return
            if self._state != "new":
                raise RuntimeError("extension runtime driver cannot be restarted")
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

    async def create_session_with_plugins(
        self,
        *,
        session_id: str,
        user_id: str,
        state: Mapping[str, Any],
        plugins: Sequence[Mapping[str, Any]],
    ) -> SessionRecord:
        """Start application extensions before accepting session Agent Plugins."""

        await self.start()
        from .dynamic_agent_plugins import forward_session_plugins

        return await forward_session_plugins(
            self._driver,
            session_id=session_id,
            user_id=user_id,
            state=state,
            plugins=plugins,
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
        """Start extensions before forwarding one bounded session page."""

        await self.start()
        if after is None and limit is None:
            return await self._driver.list_sessions(user_id=user_id)
        return await self._driver.list_sessions(
            user_id=user_id, after=after, limit=limit
        )

    async def get_session_messages(
        self, *, session_id: str, user_id: str
    ) -> Sequence[SessionMessage] | None:
        """Start extensions before reading a framework-owned transcript."""

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
        """Drain the backend before releasing extensions exactly once."""

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
    add_exception_note(
        primary,
        f"extension runtime cleanup also failed with {type(cleanup).__name__}"
    )
    return primary


__all__ = ["ExtensionHostRuntimeDriver"]
