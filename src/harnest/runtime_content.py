"""Runtime wrapper for authoritative multimodal output validation."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from .assets import AssetScope, AssetStore
from .content_validation import resolve_model_content
from .stored_media import stage_stored_media
from .runtime_contract import (
    AgentInfo,
    InvocationRequest,
    InvocationResult,
    RuntimeDriver,
    RuntimeEvent,
    SessionMessage,
    SessionRecord,
)


class ContentRuntimeDriver(RuntimeDriver):
    """Apply authored output constraints before any public transport emits data."""

    def __init__(
        self,
        inner: RuntimeDriver,
        store: AssetStore,
        asset_stores: Mapping[str, AssetStore] | None = None,
    ) -> None:
        self._inner = inner
        self._store = store
        self._stores = dict(asset_stores or {})
        self._stores.setdefault("default", store)

    @property
    def info(self) -> AgentInfo:
        return self._inner.info

    async def create_session(
        self, *, session_id: str, user_id: str, state: Mapping[str, Any]
    ) -> SessionRecord:
        return await self._inner.create_session(
            session_id=session_id, user_id=user_id, state=state
        )

    async def get_session(
        self, *, session_id: str, user_id: str
    ) -> SessionRecord | None:
        return await self._inner.get_session(session_id=session_id, user_id=user_id)

    async def list_sessions(
        self,
        *,
        user_id: str,
        after: str | None = None,
        limit: int | None = None,
    ) -> Sequence[SessionRecord]:
        return await self._inner.list_sessions(
            user_id=user_id, after=after, limit=limit
        )

    async def get_session_messages(
        self, *, session_id: str, user_id: str
    ) -> Sequence[SessionMessage] | None:
        return await self._inner.get_session_messages(
            session_id=session_id, user_id=user_id
        )

    async def update_session(
        self,
        *,
        session_id: str,
        user_id: str,
        state_delta: Mapping[str, Any],
    ) -> SessionRecord | None:
        return await self._inner.update_session(
            session_id=session_id, user_id=user_id, state_delta=state_delta
        )

    async def delete_session(self, *, session_id: str, user_id: str) -> bool:
        return await self._inner.delete_session(session_id=session_id, user_id=user_id)

    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        """Resolve final media references before JSON response publication."""

        result = await self._inner.invoke(request)
        resolved = await self._resolve_output(result.result, request)
        return InvocationResult(
            text=result.text,
            events=result.events,
            result=resolved,
            session_id=result.session_id,
            metadata=result.metadata,
        )

    async def stream(
        self, request: InvocationRequest
    ) -> AsyncIterator[RuntimeEvent]:
        """Resolve terminal output events before SSE or WebSocket publication."""

        async for event in self._inner.stream(request):
            event_type = event.get("type")
            if event_type == "graph_output":
                source = event.get("output", event.get("result"))
                resolved = await self._resolve_output(source, request)
                yield {**event, "output": resolved, "result": resolved}
            elif event_type == "output":
                resolved = await self._resolve_output(event.get("value"), request)
                yield {
                    **event,
                    "value": resolved,
                }
            else:
                yield event

    async def close(self) -> None:
        await self._inner.close()

    async def _resolve_output(self, value: Any, request: InvocationRequest) -> Any:
        """Revalidate a structured result and apply trusted asset metadata."""

        schema = self.info.output_schema
        if schema is None or value is None:
            return value
        model = value if isinstance(value, schema) else schema.model_validate(value)
        resolved = await resolve_model_content(
            model,
            store=self._store,
            stores=self._stores,
            scope=AssetScope(request.user_id, request.session_id),
        )
        staged = await stage_stored_media(
            resolved,
            stores=self._stores,
            scope=AssetScope(request.user_id, request.session_id),
        )
        return staged.model_dump(mode="json", by_alias=True)


__all__ = ["ContentRuntimeDriver"]
