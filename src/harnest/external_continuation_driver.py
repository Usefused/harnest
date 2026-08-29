"""Runtime-driver attachment for application-owned external continuations."""

from __future__ import annotations

from typing import Any, AsyncIterator, Mapping, Sequence

from .external_continuation import ExternalContinuationRuntime
from .runtime_contract import (
    AgentInfo,
    InvocationRequest,
    InvocationResult,
    RuntimeDriver,
    RuntimeEvent,
    SessionMessage,
    SessionRecord,
)


class ExternalContinuationRuntimeDriver(RuntimeDriver):
    """Expose one continuation runtime without leaking it into backend adapters."""

    def __init__(
        self, driver: RuntimeDriver, continuations: ExternalContinuationRuntime
    ) -> None:
        if not isinstance(continuations, ExternalContinuationRuntime):
            raise TypeError("continuations must be ExternalContinuationRuntime")
        self._driver = driver
        self.external_continuations = continuations

    @property
    def info(self) -> AgentInfo:
        return self._driver.info

    async def start(self) -> None:
        """Start the existing pipeline; continuation state uses its live store."""

        starter = getattr(self._driver, "start", None)
        if callable(starter):
            await starter()

    async def create_session(
        self, *, session_id: str, user_id: str, state: Mapping[str, Any]
    ) -> SessionRecord:
        return await self._driver.create_session(
            session_id=session_id, user_id=user_id, state=state
        )

    async def get_session(
        self, *, session_id: str, user_id: str
    ) -> SessionRecord | None:
        return await self._driver.get_session(session_id=session_id, user_id=user_id)

    async def list_sessions(
        self,
        *,
        user_id: str,
        after: str | None = None,
        limit: int | None = None,
    ) -> Sequence[SessionRecord]:
        """Preserve the bounded session page contract of the wrapped pipeline."""

        if after is None and limit is None:
            return await self._driver.list_sessions(user_id=user_id)
        return await self._driver.list_sessions(
            user_id=user_id, after=after, limit=limit
        )

    async def get_session_messages(
        self, *, session_id: str, user_id: str
    ) -> Sequence[SessionMessage] | None:
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
        return await self._driver.update_session(
            session_id=session_id, user_id=user_id, state_delta=state_delta
        )

    async def delete_session(self, *, session_id: str, user_id: str) -> bool:
        return await self._driver.delete_session(
            session_id=session_id, user_id=user_id
        )

    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        return await self._driver.invoke(request)

    async def stream(
        self, request: InvocationRequest
    ) -> AsyncIterator[RuntimeEvent]:
        async for event in self._driver.stream(request):
            yield event

    async def close(self) -> None:
        """Cancel local waiters before storage and plugin authority disappear."""

        failure: BaseException | None = None
        try:
            await self.external_continuations.close()
        except BaseException as error:
            failure = error
        try:
            await self._driver.close()
        except BaseException as error:
            if failure is None:
                failure = error
            else:
                failure.add_note(
                    "runtime cleanup also failed with " f"{type(error).__name__}"
                )
        if failure is not None:
            raise failure


__all__ = ["ExternalContinuationRuntimeDriver"]
