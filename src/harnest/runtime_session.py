"""Runtime ownership for lifecycle-created storage resources."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, AsyncIterator, Iterator, Mapping, Sequence

from ._exception_notes import add_exception_note
from .checkpoint import HarnestStore, RunScope
from .output import OutputPolicy
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


_DURABLE_COMPLETION_OWNER: ContextVar[str | None] = ContextVar(
    "harnest_durable_completion_owner", default=None
)


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
        output_policy: OutputPolicy | None = None,
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
        self._checkpoint_store = _harnest_checkpoint_store(
            store, checkpoint_provider, storage_registry
        )
        self._output_policy = output_policy or OutputPolicy()
        if not isinstance(self._output_policy, OutputPolicy):
            raise TypeError("output_policy must be OutputPolicy")
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
        if self._checkpoint_store is None:
            return await self._driver.invoke(request)
        try:
            with _defer_durable_completion(request.invocation_id):
                result = await self._driver.invoke(request)
            await self._complete_run(request, result)
            return result
        except BaseException:
            await self._fail_run(request)
            raise

    async def stream(
        self, request: InvocationRequest
    ) -> AsyncIterator[RuntimeEvent]:
        await self.start()
        if self._checkpoint_store is None:
            async for event in self._driver.stream(request):
                yield event
            return
        events: list[RuntimeEvent] = []
        iterator = self._driver.stream(request).__aiter__()
        try:
            while True:
                try:
                    event = await _next_owned(iterator, request.invocation_id)
                except StopAsyncIteration:
                    break
                events.append(event)
                yield event
            await self._complete_run(
                request, _stream_invocation_result(request, events)
            )
        except BaseException:
            await self._fail_run(request)
            raise
        finally:
            await _close_owned(iterator, request.invocation_id)

    async def _complete_run(
        self, request: InvocationRequest, result: InvocationResult
    ) -> None:
        """Persist public completion data before publishing terminal run state."""

        from .checkpoint import DurableRunResult, put_durable_run_result

        store = self._checkpoint_store
        if store is None:
            return
        scope = self._run_scope(request, result.session_id)
        current = await store.get_run(scope=scope)
        if current is None or current.status != "running":
            return
        durable = DurableRunResult.capture(
            result.text,
            result.events,
            result.result,
            result.metadata,
            persist_raw=self._output_policy.persist_raw_agent_metadata,
        )
        await put_durable_run_result(store, scope=scope, result=durable)
        await store.transition(
            scope=scope, expected_status="running", status="completed"
        )

    async def _fail_run(self, request: InvocationRequest) -> None:
        """Release a still-running durable run after outer finalization fails."""

        store = self._checkpoint_store
        if store is None:
            return
        scope = self._run_scope(request, request.session_id)
        current = await store.get_run(scope=scope)
        if current is not None and current.status == "running":
            await store.transition(
                scope=scope, expected_status="running", status="failed"
            )

    def _run_scope(self, request: InvocationRequest, session_id: str) -> RunScope:
        """Use stable application and caller ownership for the result checkpoint."""

        return RunScope(
            self.info.id,
            request.user_id,
            session_id,
            request.invocation_id,
        )

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
    add_exception_note(
        primary, f"{label} cleanup also failed with {type(cleanup).__name__}"
    )
    return primary


def _unique_resources(*values: Any) -> tuple[Any, ...]:
    """Preserve ownership order while removing identical shared stores."""

    result: list[Any] = []
    for value in values:
        if value is not None and all(value is not item for item in result):
            result.append(value)
    return tuple(result)


def durable_completion_deferred(invocation_id: str) -> bool:
    """Report whether the outer storage wrapper owns this run's completion."""

    return _DURABLE_COMPLETION_OWNER.get() == invocation_id


@contextmanager
def _defer_durable_completion(invocation_id: str) -> Iterator[None]:
    """Delegate only one invocation's terminal transition to the outer wrapper."""

    token = _DURABLE_COMPLETION_OWNER.set(invocation_id)
    try:
        yield
    finally:
        _DURABLE_COMPLETION_OWNER.reset(token)


async def _next_owned(iterator: Any, invocation_id: str) -> RuntimeEvent:
    """Advance a backend only while its matching durable owner is active."""

    with _defer_durable_completion(invocation_id):
        return await iterator.__anext__()


async def _close_owned(iterator: Any, invocation_id: str) -> None:
    """Close a backend iterator under the same invocation-scoped ownership."""

    close = getattr(iterator, "aclose", None)
    if callable(close):
        with _defer_durable_completion(invocation_id):
            await close()


def _stream_invocation_result(
    request: InvocationRequest, events: Sequence[RuntimeEvent]
) -> InvocationResult:
    """Rebuild the public stream result for durable completion persistence."""

    text = "".join(
        str(event.get("text", ""))
        for event in events
        if event.get("type") == "message"
    )
    result = next(
        (
            _output_event_value(event)
            for event in reversed(events)
            if event.get("type") in {"graph_output", "output"}
        ),
        None,
    )
    return InvocationResult(
        text=text,
        events=tuple(events),
        result=result,
        session_id=request.session_id,
        metadata=dict(request.metadata),
    )


def _output_event_value(event: Mapping[str, Any]) -> Any:
    """Read the final structured value from either portable output event."""

    if "result" in event:
        return event["result"]
    if event.get("type") == "graph_output":
        return event.get("output")
    return event.get("value")


def _harnest_checkpoint_store(
    session_store: Any,
    checkpoint_provider: Any,
    registry: StorageRegistry | None,
) -> HarnestStore | None:
    """Select only a portable store that can own invocation-result checkpoints."""

    candidates = (
        registry.checkpoints if registry is not None else None,
        checkpoint_provider,
        session_store,
    )
    return next(
        (item for item in candidates if isinstance(item, HarnestStore)), None
    )


__all__ = ["StorageRuntimeDriver"]
