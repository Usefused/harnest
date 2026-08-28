"""LangGraph implementation of Harnest's framework-neutral runtime driver.

This module deliberately contains no HTTP concerns.  It translates between the
portable runtime contract and LangGraph's state/message protocol while keeping
the public Harnest session state authoritative.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextvars
import inspect
import json
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import BaseModel

from ._json import json_value
from .approval import (
    ApprovalPolicy,
    authorize_mcp,
    record_approved_execution,
    record_approved_failure,
)
from .application import CompiledApplication
from .assets import AssetScope, AssetStore
from .asset_inspection import inspect_asset
from .checkpoint import CheckpointStore, HarnestStore
from .content import AssetRef, Audio, Data, File, Image, Text, Video
from .graph import _model_input_text
from .model_lifecycle import close_litellm_lifecycles
from .mcp import _validate_approval_tools
from .mcp_lifecycle import (
    _MCPClientLifecycleBinding,
    close_mcp_lifecycles,
    start_mcp_lifecycles,
)
from .runtime_contract import (
    AgentInfo,
    InvocationRequest,
    InvocationResult,
    RuntimeDriver,
    SessionMessage,
    SessionRecord,
)
from .output import OutputPolicy
from .session import InMemorySessionStore, SessionLease, SessionStore
from .structured import validate_runtime_output


_TURN_START_KEY = "_harnest_turn_start"
_SESSION_STATE_KEY = "_harnest_state"
_GRAPH_RUNTIME_KEYS = frozenset(
    {"messages", "value", "route", _TURN_START_KEY, _SESSION_STATE_KEY}
)
_MODEL_ASSET_SCOPE: contextvars.ContextVar[AssetScope | None] = (
    contextvars.ContextVar("harnest_langgraph_asset_scope", default=None)
)
_CONTENT_PART_TYPES = (Text, Image, Audio, Video, File, Data, AssetRef)
_NO_ORDINARY_CONTENT = object()


@dataclass(slots=True)
class _StreamState:
    final_state: Any = None
    text: str = ""
    tools: set[tuple[Any, ...]] = field(default_factory=set)


class LangGraphRuntimeDriver(RuntimeDriver):
    """Adapt one compiled LangGraph application to the neutral runtime.

    The driver owns the development store or uses one injected production
    store. Every graph execution receives a fresh internal ``thread_id`` so an
    optional LangGraph checkpointer cannot become a second session authority.
    """

    def __init__(
        self,
        application: CompiledApplication,
        *,
        card: Mapping[str, Any] | None = None,
        recursion_limit: int = 64,
        session_store: SessionStore | None = None,
        asset_store: AssetStore | None = None,
    ) -> None:
        plan = _runtime_plan(application, recursion_limit)
        card_value = dict(card or {})
        self._application = application
        self._plan = plan
        self._target = None if plan is not None else application.target
        self._mcp_clients: list[Any] = []
        self._mcp_lifecycles: list[_MCPClientLifecycleBinding] = []
        self._materialize_lock = asyncio.Lock()
        self._recursion_limit = recursion_limit
        self._info = _agent_info(application, card_value)
        self._session_store = (
            session_store
            if session_store is not None
            else InMemorySessionStore()
        )
        self._owns_session_store = session_store is None
        if not isinstance(self._session_store, SessionStore):
            raise TypeError("session_store must implement SessionStore")
        configured_assets = (
            asset_store
            if asset_store is not None
            else getattr(application, "asset_store", None)
        )
        if configured_assets is not None and not isinstance(
            configured_assets, AssetStore
        ):
            raise TypeError("asset_store must implement AssetStore")
        self._asset_store = configured_assets
        self._closed = False

    @property
    def info(self) -> AgentInfo:
        return self._info

    async def create_session(
        self,
        *,
        session_id: str,
        user_id: str,
        state: Mapping[str, Any],
    ) -> SessionRecord:
        self._ensure_open()
        record = await self._session_store.create(
            session_id=session_id,
            user_id=user_id,
            state=state,
        )
        return self._public_session(record)

    async def get_session(
        self, *, session_id: str, user_id: str
    ) -> SessionRecord | None:
        self._ensure_open()
        record = await self._session_store.get(
            session_id=session_id,
            user_id=user_id,
        )
        return None if record is None else self._public_session(record)

    async def list_sessions(
        self,
        *,
        user_id: str,
        after: str | None = None,
        limit: int | None = None,
    ) -> Sequence[SessionRecord]:
        """Project an ordered store-level keyset page into public records."""

        self._ensure_open()
        if after is None and limit is None:
            records = await self._session_store.list(user_id=user_id)
        else:
            records = await self._session_store.list(
                user_id=user_id, after=after, limit=limit
            )
        return tuple(self._public_session(record) for record in records)

    async def get_session_messages(
        self, *, session_id: str, user_id: str
    ) -> Sequence[SessionMessage] | None:
        """Return the stored transcript without exposing Harnest state keys."""

        self._ensure_open()
        record = await self._session_store.get(
            session_id=session_id,
            user_id=user_id,
        )
        return None if record is None else _langgraph_session_messages(record)

    async def update_session(
        self,
        *,
        session_id: str,
        user_id: str,
        state_delta: Mapping[str, Any],
    ) -> SessionRecord | None:
        self._ensure_open()
        if self._application.kind != "graph":
            return await self._session_store.update(
                session_id=session_id,
                user_id=user_id,
                state_delta=state_delta,
            )
        async with self._session_store.acquire(
            session_id=session_id, user_id=user_id
        ) as session:
            state = _managed_graph_user_state(session.record.state)
            state.update(state_delta)
            internal = _managed_graph_internal_state(session.record.state, state)
            record = await session.replace_state(internal)
        return self._public_session(record)

    async def delete_session(self, *, session_id: str, user_id: str) -> bool:
        self._ensure_open()
        return await self._session_store.delete(
            session_id=session_id,
            user_id=user_id,
        )

    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        """Run one graph invocation and return canonical neutral events."""

        session_id = await self._session_id_for_request(request)
        async with self._session_store.acquire(
            session_id=session_id, user_id=request.user_id
        ) as session:
            await self._apply_request_delta(session, request)
            graph_input = _graph_input(
                self._application, request.input, session.record.state
            )
            await self._begin_checkpoint(request, session_id)
            config = self._execution_config(request.invocation_id)
            target = await self._target_for_run()
            scope_token = _MODEL_ASSET_SCOPE.set(
                AssetScope(user_id=request.user_id, session_id=session_id)
            )
            try:
                result = await target.ainvoke(graph_input, config=config)
                await self._store_result(
                    session,
                    result,
                    scope=AssetScope(
                        user_id=request.user_id, session_id=session_id
                    ),
                )
            except BaseException:
                await self._finish_checkpoint(request.invocation_id, "failed")
                raise
            finally:
                _MODEL_ASSET_SCOPE.reset(scope_token)
            await self._finish_checkpoint(request.invocation_id, "completed")

        turn_start = _message_count(graph_input)
        text_value, public_result = _graph_output(
            self._application, result, turn_start=turn_start
        )
        events = _result_events(
            result,
            text_value,
            public_result,
            self._application.output_policy,
            turn_start=turn_start,
        )
        visible_text = _result_text(events, fallback=text_value)
        return InvocationResult(
            text=visible_text,
            events=tuple(events),
            result=public_result,
            session_id=session_id,
            metadata=dict(request.metadata),
        )

    async def stream(
        self, request: InvocationRequest
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield visible LangGraph output as canonical neutral events."""

        target = await self._target_for_run()
        if not callable(getattr(target, "astream", None)):
            result = await self.invoke(request)
            for event in result.events:
                yield dict(event)
            return

        session_id = await self._session_id_for_request(request)
        async with self._session_store.acquire(
            session_id=session_id, user_id=request.user_id
        ) as session:
            await self._apply_request_delta(session, request)
            graph_input = _graph_input(
                self._application, request.input, session.record.state
            )
            await self._begin_checkpoint(request, session_id)
            config = self._execution_config(request.invocation_id)
            state = _StreamState()
            scope = AssetScope(user_id=request.user_id, session_id=session_id)
            scope_token = _MODEL_ASSET_SCOPE.set(scope)
            try:
                async for event in _target_stream(
                    target,
                    graph_input,
                    config,
                    state,
                    self._application.output_policy,
                ):
                    yield event
                state.final_state = (
                    state.final_state if state.final_state is not None else {}
                )
                await self._store_result(session, state.final_state, scope=scope)
            except BaseException:
                await self._finish_checkpoint(request.invocation_id, "failed")
                raise
            finally:
                _MODEL_ASSET_SCOPE.reset(scope_token)
            await self._finish_checkpoint(request.invocation_id, "completed")

        turn_start = _message_count(graph_input)
        for event in _final_stream_events(
            self._application, state, turn_start=turn_start
        ):
            yield event

    async def close(self) -> None:
        """Close a lazily initialized backend resource, if it owns one."""

        if self._closed:
            return
        self._closed = True
        failure: BaseException | None = None
        try:
            async with self._materialize_lock:
                target = self._target
                try:
                    if target is not None:
                        await _close_resource(target)
                except BaseException as exc:
                    failure = exc
                try:
                    await self._close_mcp_resources(skip=target)
                except BaseException as exc:
                    if failure is None:
                        failure = exc
        finally:
            if self._owns_session_store:
                await self._session_store.close()
        if failure is not None:
            raise failure

    async def _target_for_run(self) -> Any:
        """Materialize MCP-backed agents during the driver's lifecycle."""

        self._ensure_open()
        if self._target is not None:
            return self._target
        async with self._materialize_lock:
            self._ensure_open()
            if self._target is not None:
                return self._target
            plan = self._plan
            if plan is None:  # pragma: no cover - protected by constructor
                raise RuntimeError("LangGraph target is unavailable")
            await self._materialize_plan(plan)
            return self._target

    async def _materialize_plan(self, plan: Any) -> None:
        """Build one target and unwind partial MCP ownership on startup failure."""

        try:
            from .backends.langgraph import (
                ManagedAgentPlan, managed_graph_mcp_clients,
                materialize_agent, materialize_graph,
            )
            configured = (
                (tuple(plan.definition.mcp),)
                if isinstance(plan, ManagedAgentPlan)
                else managed_graph_mcp_clients(plan)
            )
            tool_groups = await self._resolve_tool_groups(configured)
            runtime_plan = _plan_with_asset_middleware(plan, self._asset_store)
            self._target = (
                materialize_agent(runtime_plan, tool_groups[0])
                if isinstance(plan, ManagedAgentPlan)
                else materialize_graph(runtime_plan, tool_groups)
            )
        except BaseException as error:
            try:
                await self._close_mcp_resources(reset_lifecycles=True)
            except BaseException as cleanup_error:
                # Startup remains the actionable failure; lifecycle errors are
                # redacted and retained only as a type-level diagnostic note.
                add_note = getattr(error, "add_note", None)
                if callable(add_note):
                    add_note(
                        "MCP startup cleanup also failed with "
                        f"{type(cleanup_error).__name__}"
                    )
            raise

    async def _resolve_tool_groups(
        self, configured_groups: Sequence[tuple[Any, ...]]
    ) -> list[list[Any]]:
        """Reuse identical MCP discovery groups within one materialized graph."""

        resolved: list[tuple[tuple[Any, ...], list[Any]]] = []
        groups: list[list[Any]] = []
        for configured in configured_groups:
            # Reuse an identical MCP group so nested graph nodes do not open
            # duplicate connections or repeat remote tool discovery.
            cached = next(
                (item for item in resolved if item[0] == configured), None
            )
            if cached is None:
                tools = await self._resolve_tool_group(configured)
                cached = (configured, tools)
                resolved.append(cached)
            groups.append(cached[1])
        return groups

    async def _resolve_tool_group(
        self, configured_group: tuple[Any, ...]
    ) -> list[Any]:
        """Attach approval at transport time before exposing discovered tools."""

        await self._start_mcp_lifecycles(configured_group)
        client_type = _mcp_client_type()
        connections, names = _mcp_connections(configured_group)
        policies = _mcp_server_approval_policies(names)
        interceptors = [mcp_approval_interceptor(policies)] if policies else []
        client = client_type(
            connections,
            tool_interceptors=interceptors,
            tool_name_prefix=True,
        )
        self._mcp_clients.append(client)
        tools: list[Any] = []
        for server_name, configured in names:
            discovered = await client.get_tools(server_name=server_name)
            selected = _filtered_mcp_tools(
                discovered, server_name=server_name, allowed=configured.tool_filter
            )
            _validate_mcp_approval(selected, server_name, configured)
            tools.extend(selected)
        return tools

    async def _start_mcp_lifecycles(
        self, configured_group: Sequence[Any]
    ) -> None:
        """Start newly encountered MCP lifecycle owners before discovery."""

        owned = {id(binding.controller) for binding in self._mcp_lifecycles}
        new_bindings: list[_MCPClientLifecycleBinding] = []
        for configured in configured_group:
            binding = configured._lifecycle_binding("langgraph")
            if binding is None or id(binding.controller) in owned:
                continue
            owned.add(id(binding.controller))
            new_bindings.append(binding)
        await start_mcp_lifecycles(new_bindings)
        self._mcp_lifecycles.extend(new_bindings)

    async def _close_mcp_resources(
        self,
        *,
        skip: Any = None,
        reset_lifecycles: bool = False,
    ) -> None:
        """Close adapter sessions before their application lifecycle owners."""

        failure: BaseException | None = None
        for client in reversed(self._mcp_clients):
            try:
                if client is not skip:
                    await _close_resource(client)
            except BaseException as error:
                if failure is None:
                    failure = error
        self._mcp_clients.clear()
        try:
            await close_mcp_lifecycles(
                self._mcp_lifecycles, reset=reset_lifecycles
            )
        except BaseException as error:
            if failure is None:
                failure = error
        self._mcp_lifecycles.clear()
        if failure is not None:
            raise failure

    async def _session_id_for_request(self, request: InvocationRequest) -> str:
        self._ensure_open()
        user_id = request.user_id
        _require_identifier(user_id, "request.user_id")
        requested_id = request.session_id
        if requested_id is not None:
            _require_identifier(requested_id, "request.session_id")
            stored = await self._session_store.get(
                user_id=user_id, session_id=requested_id
            )
            if stored is None:
                raise KeyError("session not found")
            return requested_id

        session_id = uuid.uuid4().hex
        await self._session_store.create(
            user_id=user_id, session_id=session_id, state={}
        )
        return session_id

    async def _apply_request_delta(
        self, session: SessionLease, request: InvocationRequest
    ) -> None:
        delta = request.state_delta
        if not delta:
            return
        if self._application.kind != "graph":
            await session.patch_state(delta)
            return
        state = _managed_graph_user_state(session.record.state)
        state.update(delta)
        await session.replace_state(
            _managed_graph_internal_state(session.record.state, state)
        )

    async def _store_result(
        self,
        session: SessionLease,
        result: Any,
        *,
        scope: AssetScope,
    ) -> None:
        """Commit only reference-safe messages to Harnest session storage."""

        if isinstance(result, Mapping):
            # ``ainvoke`` returns the complete graph state.  Replacing instead of
            # merging also removes transient keys intentionally cleared by a graph.
            state = dict(result)
            state.pop(_TURN_START_KEY, None)
            if isinstance(state.get("messages"), (list, tuple)):
                state["messages"] = [
                    await _reference_safe_message(
                        message, store=self._asset_store, scope=scope
                    )
                    for message in state["messages"]
                ]
        else:
            state = {"value": result}
        await session.replace_state(state)

    async def _begin_checkpoint(
        self, request: InvocationRequest, session_id: str
    ) -> None:
        """Claim portable run ownership before invoking the compiled graph."""

        store = self._portable_checkpoints()
        if store is None:
            return
        await store.begin_run(
            application_id=self._application.name,
            user_id=request.user_id,
            session_id=session_id,
            run_id=request.invocation_id,
            framework="langgraph",
        )

    async def _finish_checkpoint(self, run_id: str, status: str) -> None:
        """Release portable run ownership after the graph becomes terminal."""

        store = self._portable_checkpoints()
        if store is None:
            return
        record = await store.get_run(run_id=run_id)
        if record is not None and record.status == "running":
            await store.transition(
                run_id=run_id,
                expected_status="running",
                status=status,
            )

    def _portable_checkpoints(self) -> CheckpointStore | None:
        """Use portable checkpoints only when Harnest owns the native adapter."""

        provider = self._application.checkpointer
        return provider if isinstance(provider, HarnestStore) else None

    def _execution_config(self, run_id: str) -> dict[str, Any]:
        """Isolate native checkpoint threads while SessionStore carries history."""

        return {
            "configurable": {
                # A thread represents one invocation. SessionStore remains the
                # committed multi-turn authority across these isolated runs.
                "thread_id": run_id,
            },
            "recursion_limit": self._recursion_limit,
        }

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("LangGraph runtime driver is closed")

    def _public_session(self, record: SessionRecord) -> SessionRecord:
        """Separate portable state from lossless framework-owned session data."""

        metadata = {
            **dict(record.metadata),
            "langgraph": json_value(record.state),
        }
        if self._application.kind == "advanced":
            return replace(record, metadata=metadata)
        return replace(
            record,
            state=_managed_graph_user_state(record.state),
            metadata=metadata,
        )


def _runtime_plan(application: Any, recursion_limit: int) -> Any | None:
    if not isinstance(application, CompiledApplication):
        raise TypeError("application must be a CompiledApplication")
    if application.framework != "langgraph":
        raise ValueError("LangGraphRuntimeDriver requires a LangGraph application")
    if recursion_limit < 1:
        raise ValueError("recursion_limit must be at least one")
    from .backends.langgraph import ManagedAgentPlan, ManagedGraphPlan

    if isinstance(application.target, (ManagedAgentPlan, ManagedGraphPlan)):
        return application.target
    if not callable(getattr(application.target, "ainvoke", None)):
        raise TypeError("compiled LangGraph target must expose ainvoke")
    return None


def _plan_with_asset_middleware(plan: Any, store: AssetStore | None) -> Any:
    """Attach byte materialization as the innermost managed-model boundary."""

    if store is None:
        return plan
    middleware = tuple(getattr(plan, "middleware", ()))
    # Appending keeps extension middleware outside this adapter so hooks and
    # telemetry observe references, while only the provider sees byte content.
    return replace(
        plan,
        middleware=(*middleware, _langgraph_asset_middleware(store)),
    )


def _langgraph_asset_middleware(store: AssetStore) -> Any:
    """Build middleware that swaps scoped references only around model I/O."""

    from langchain.agents.middleware import AgentMiddleware

    class AssetBoundaryMiddleware(AgentMiddleware):
        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            scope = _MODEL_ASSET_SCOPE.get()
            if scope is None:
                if _messages_have_asset_references(request.messages):
                    raise RuntimeError("asset model call requires invocation scope")
                return await handler(request)
            messages = [
                await _materialize_model_message(message, store=store, scope=scope)
                for message in request.messages
            ]
            response = await handler(request.override(messages=messages))
            return await _reference_safe_model_response(
                response, store=store, scope=scope
            )

        def wrap_model_call(self, request: Any, handler: Any) -> Any:
            if _messages_have_asset_references(request.messages):
                raise RuntimeError(
                    "asset-backed LangGraph calls require asynchronous invocation"
                )
            return handler(request)

    return AssetBoundaryMiddleware()


def _messages_have_asset_references(messages: Sequence[Any]) -> bool:
    """Detect portable references without inspecting or reflecting their IDs."""

    return any(
        _content_has_asset_reference(getattr(message, "content", None))
        for message in messages
    )


def _content_has_asset_reference(content: Any) -> bool:
    """Recognize reference blocks in one native message content value."""

    if not isinstance(content, (list, tuple)):
        return False
    return any(
        isinstance(block, Mapping)
        and isinstance(block.get("assetId") or block.get("asset_id"), str)
        for block in content
    )


async def _materialize_model_message(
    message: Any, *, store: AssetStore, scope: AssetScope
) -> Any:
    """Clone a message with provider-ready bytes for this model call only."""

    content = getattr(message, "content", None)
    if not _content_has_asset_reference(content):
        return message
    blocks = [
        await _materialize_model_block(block, store=store, scope=scope)
        for block in content
    ]
    copier = getattr(message, "model_copy", None)
    if not callable(copier):
        raise TypeError("LangChain messages must support model_copy")
    return copier(update={"content": blocks})


async def _materialize_model_block(
    block: Any, *, store: AssetStore, scope: AssetScope
) -> Any:
    """Resolve one opaque asset into a standard LangChain content block."""

    if not isinstance(block, Mapping):
        return block
    asset_id = block.get("assetId") or block.get("asset_id")
    if not isinstance(asset_id, str):
        return dict(block)
    record = await store.stat(scope=scope, asset_id=asset_id)
    if record is None:
        raise ValueError("content asset is unavailable")
    chunks = [chunk async for chunk in store.open(scope=scope, asset_id=asset_id)]
    kind = _provider_content_kind(str(block.get("type")), record.media_type)
    return {
        "type": kind,
        "base64": base64.b64encode(b"".join(chunks)).decode("ascii"),
        "mime_type": record.media_type,
    }


def _provider_content_kind(kind: str, media_type: str) -> str:
    """Resolve generic asset references to a provider-neutral media family."""

    if kind != "asset":
        return kind
    family = media_type.partition("/")[0]
    return family if family in {"image", "audio", "video"} else "file"


async def _reference_safe_model_response(
    response: Any, *, store: AssetStore, scope: AssetScope
) -> Any:
    """Stage inline model output before LangGraph can checkpoint the response."""

    result = getattr(response, "result", None)
    if not isinstance(result, list):
        return response
    messages = [
        await _reference_safe_output_message(message, store=store, scope=scope)
        for message in result
    ]
    return replace(response, result=messages)


async def _reference_safe_output_message(
    message: Any, *, store: AssetStore, scope: AssetScope
) -> Any:
    """Replace inline output media while preserving the LangChain message type."""

    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return message
    blocks = await _reference_safe_blocks(content, store=store, scope=scope)
    copier = getattr(message, "model_copy", None)
    if not callable(copier):
        raise TypeError("LangChain messages must support model_copy")
    return copier(update={"content": blocks})


def _agent_info(application: CompiledApplication, card: dict[str, Any]) -> AgentInfo:
    return AgentInfo(
        id=application.name,
        name=str(card.get("name") or application.name),
        description=str(card.get("description") or ""),
        card=card,
        framework="langgraph",
        mode=application.mode,
        input_schema=application.input_schema,
        output_schema=application.output_schema,
    )


async def _target_stream(
    target: Any,
    graph_input: Any,
    config: Mapping[str, Any],
    state: _StreamState,
    output_policy: OutputPolicy,
) -> AsyncIterator[dict[str, Any]]:
    """Normalize target events while keeping tool traces independently visible."""

    stream = target.astream(graph_input, config=config, stream_mode=["messages", "values"])
    async with aclosing(stream):
        async for item in stream:
            mode, value = _stream_item(item)
            if mode == "values":
                state.final_state = value
                continue
            message = value[0] if isinstance(value, tuple) else value
            tool_events = _message_tool_events(message)
            for event in tool_events:
                identity = _event_identity(event)
                if identity not in state.tools:
                    state.tools.add(identity)
                    yield event
            content = _message_text(message)
            has_tool_calls = any(
                event.get("type") == "tool_call" for event in tool_events
            )
            if content and output_policy.includes_intermediate_message(
                has_tool_calls=has_tool_calls
            ):
                # Included narration is public but is not part of the canonical
                # reply used to reconcile the graph's final values event.
                if not has_tool_calls:
                    state.text += content
                yield {"type": "message", "role": "assistant", "text": content}


def _final_stream_events(
    application: CompiledApplication,
    state: _StreamState,
    *,
    turn_start: int,
) -> list[dict[str, Any]]:
    final_text, public_result = _graph_output(
        application, state.final_state, turn_start=turn_start
    )
    events: list[dict[str, Any]] = []
    if final_text and final_text != state.text:
        delta = (
            final_text[len(state.text) :]
            if final_text.startswith(state.text)
            else final_text
        )
        if delta:
            events.append({"type": "message", "role": "assistant", "text": delta})
    for event in _tool_events(state.final_state):
        identity = _event_identity(event)
        if identity not in state.tools:
            state.tools.add(identity)
            events.append(event)
    if public_result is not None:
        events.append(
            {
                "type": "graph_output",
                "output": public_result,
                "result": public_result,
            }
        )
    return events


def _mcp_client_type() -> Any:
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError as exc:  # pragma: no cover - optional backend
        raise RuntimeError(
            "LangGraph MCP connections require langchain-mcp-adapters"
        ) from exc
    return MultiServerMCPClient


def _mcp_connections(
    configured_group: Sequence[Any],
) -> tuple[dict[str, dict[str, Any]], list[tuple[str, Any]]]:
    """Build one deduplicated adapter connection map and approval-policy index."""

    connections: dict[str, dict[str, Any]] = {}
    names: list[tuple[str, Any]] = []
    for index, configured in enumerate(configured_group):
        name = (
            configured.capability_id
            or configured.identity
            or configured.tool_name_prefix
            or f"mcp_{index + 1}"
        )
        if name in connections:
            raise ValueError(f"duplicate LangGraph MCP client name {name!r}")
        names.append((name, configured))
        connections[name] = configured.to_langgraph_connection()
    return connections, names


def _mcp_server_approval_policies(
    configured: Sequence[tuple[str, Any]],
) -> dict[str, tuple[str, ApprovalPolicy]]:
    """Index policies by adapter server identity before tool execution exists."""

    return {
        server_name: (server_name, client.approval)
        for server_name, client in configured
        if client.approval is not None
    }


def _validate_mcp_approval(
    tools: Sequence[Any], server_name: str, configured: Any
) -> None:
    """Fail startup when approval selects tools the server did not expose."""

    policy = configured.approval
    if policy is None:
        return
    prefix = f"{server_name}_"
    remote_names = tuple(
        str(getattr(tool, "name", "")).removeprefix(prefix) for tool in tools
    )
    _validate_approval_tools(policy, remote_names, capability_id=server_name)


def mcp_approval_interceptor(
    policies: Mapping[str, tuple[str, ApprovalPolicy]],
) -> Any:
    """Gate MCP transport calls using the adapter's original server/tool names."""

    async def require_approval(request: Any, handler: Any) -> Any:
        """Authorize selected remote calls immediately before MCP execution."""

        configured = policies.get(str(request.server_name))
        if configured is None:
            return await handler(request)
        client_name, policy = configured
        tool_name = str(request.name)
        if not policy.applies_to(tool_name):
            return await handler(request)
        grant = await authorize_mcp(client_name, tool_name, request.args, policy)
        try:
            result = await handler(request)
        except BaseException:
            record_approved_failure(grant)
            raise
        if _mcp_result_failed(result):
            record_approved_failure(grant)
        else:
            record_approved_execution(grant)
        return result

    return require_approval


def _mcp_result_failed(result: Any) -> bool:
    """Recognize adapter-level error results before they become ToolMessages."""

    return getattr(result, "isError", False) is True or getattr(
        result, "status", None
    ) == "error"


def _require_identifier(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _filtered_mcp_tools(
    discovered: Sequence[Any],
    *,
    server_name: str,
    allowed: Sequence[str] | None,
) -> list[Any]:
    tools = list(discovered)
    if allowed is None:
        return tools
    allowed_names = set(allowed)
    prefix = f"{server_name}_"
    tools = [
        tool
        for tool in tools
        if getattr(tool, "name", None) in allowed_names
        or str(getattr(tool, "name", "")).removeprefix(prefix) in allowed_names
    ]
    resolved = set()
    for tool in tools:
        name = str(getattr(tool, "name", ""))
        resolved.update((name, name.removeprefix(prefix)))
    missing = sorted(allowed_names - resolved)
    if missing:
        raise ValueError(
            f"MCP server {server_name!r} did not expose filtered tools: "
            f"{', '.join(missing)}"
        )
    return tools


async def _close_resource(resource: Any) -> None:
    try:
        closer = getattr(resource, "aclose", None)
        if not callable(closer):
            closer = getattr(resource, "close", None)
        if callable(closer):
            result = closer()
            if inspect.isawaitable(result):
                await result
    finally:
        await close_litellm_lifecycles(resource)


def _graph_input(
    application: CompiledApplication,
    text_value: Any,
    state: Mapping[str, Any],
) -> Any:
    """Build graph state with reference-only content safe for checkpointing."""

    if application.bridge is not None and application.bridge.input_adapter is not None:
        return application.bridge.input_adapter(text_value, state)
    try:
        from langchain_core.messages import HumanMessage
    except ImportError as exc:  # pragma: no cover - optional backend dependency
        raise RuntimeError("LangGraph serving requires langchain-core") from exc
    previous_messages = state.get("messages", ())
    if not isinstance(previous_messages, (list, tuple)):
        previous_messages = ()
    messages = [
        *previous_messages,
        HumanMessage(
            content=_portable_input_content(
                _managed_authored_input(application, text_value)
            )
        ),
    ]
    if application.kind == "graph":
        return {
            **{
                key: value
                for key, value in state.items()
                if key in _GRAPH_RUNTIME_KEYS
            },
            _SESSION_STATE_KEY: _managed_graph_user_state(state),
            "value": text_value,
            "messages": messages,
            "route": None,
            _TURN_START_KEY: len(previous_messages),
        }
    return {**dict(state), "messages": messages}


def _managed_authored_input(
    application: CompiledApplication, value: Any
) -> Any:
    """Restore authored model types after the neutral JSON transport boundary."""

    schema = application.input_schema
    if schema is None or isinstance(value, schema):
        return value
    return schema.model_validate(value)


def _portable_input_content(value: Any) -> Any:
    """Preserve authored content order while retaining legacy text inputs."""

    if isinstance(value, _CONTENT_PART_TYPES):
        return [_portable_content_block(value)]
    if isinstance(value, (list, tuple)) and value and all(
        isinstance(item, _CONTENT_PART_TYPES) for item in value
    ):
        return [_portable_content_block(item) for item in value]
    if isinstance(value, BaseModel):
        blocks = _model_content_blocks(value)
        if blocks:
            return blocks
    return _model_input_text(value)


def _model_content_blocks(value: BaseModel) -> list[dict[str, Any]]:
    """Project content fields and serialize remaining structured input as text."""

    ordinary, blocks = _extract_content(value)
    if not blocks:
        return []
    if ordinary is not _NO_ORDINARY_CONTENT:
        blocks.insert(
            0,
            {"type": "text", "text": _model_input_text(ordinary)},
        )
    return blocks


def _extract_content(value: Any) -> tuple[Any, list[dict[str, Any]]]:
    """Separate nested authored content from its remaining JSON structure."""

    if isinstance(value, _CONTENT_PART_TYPES):
        return _NO_ORDINARY_CONTENT, [_portable_content_block(value)]
    if isinstance(value, BaseModel):
        fields = (
            (name, getattr(value, name)) for name in type(value).model_fields
        )
        return _extract_content_mapping(fields)
    if isinstance(value, Mapping):
        return _extract_content_mapping(value.items())
    if isinstance(value, (list, tuple)) and value:
        return _extract_content_sequence(value)
    return json_value(value), []


def _extract_content_mapping(values: Any) -> tuple[Any, list[dict[str, Any]]]:
    """Walk mapping fields in author order and retain their names in text data."""

    ordinary: dict[str, Any] = {}
    blocks: list[dict[str, Any]] = []
    for name, value in values:
        projected, nested = _extract_content(value)
        if projected is not _NO_ORDINARY_CONTENT:
            ordinary[str(name)] = projected
        blocks.extend(nested)
    return (ordinary if ordinary else _NO_ORDINARY_CONTENT), blocks


def _extract_content_sequence(
    values: Sequence[Any],
) -> tuple[Any, list[dict[str, Any]]]:
    """Walk nested sequences without changing the order of content parts."""

    ordinary: list[Any] = []
    blocks: list[dict[str, Any]] = []
    for value in values:
        projected, nested = _extract_content(value)
        if projected is not _NO_ORDINARY_CONTENT:
            ordinary.append(projected)
        blocks.extend(nested)
    return (ordinary if ordinary else _NO_ORDINARY_CONTENT), blocks


def _portable_content_block(part: Any) -> dict[str, Any]:
    """Serialize one authored part without introducing provider representations."""

    return part.model_dump(mode="json", by_alias=True, exclude_none=True)


def _graph_output(
    application: CompiledApplication,
    result: Any,
    *,
    turn_start: int = 0,
) -> tuple[str, Any]:
    """Return public output after Harnest enriches any declared metadata field."""

    adapter = (
        application.bridge.output_adapter
        if application.bridge is not None
        else None
    )
    adapted = adapter(result) if adapter is not None else result
    if adapted is not result:
        return _adapted_output(adapted)
    if isinstance(result, Mapping):
        if application.output_schema is not None:
            model = validate_runtime_output(
                application.output_schema,
                _structured_output_value(application, result),
                metadata=_langgraph_turn_metadata(result, turn_start=turn_start),
                boundary="model output",
            )
            structured = json_value(model)
            return _visible_value(structured), structured
        return _mapping_output(result)
    if isinstance(result, str):
        return result, result
    return _visible_value(result), json_value(result)


def _structured_output_value(
    application: CompiledApplication, result: Mapping[str, Any]
) -> Any:
    """Select the provider result for agents and the terminal value for graphs."""

    if getattr(application, "kind", "agent") == "graph":
        return result.get("value")
    return result.get("structured_response")


def _langgraph_turn_metadata(result: Any, *, turn_start: int) -> dict[str, Any]:
    """Preserve native current-turn state under an explicit framework namespace."""

    if not isinstance(result, Mapping):
        return {"langgraph": {"result": json_value(result)}}
    native = {
        key: value
        for key, value in result.items()
        if key not in {_TURN_START_KEY, _SESSION_STATE_KEY}
    }
    messages = result.get("messages")
    if isinstance(messages, (list, tuple)):
        # Session history remains durable, while output metadata describes only
        # the turn that produced this result and its framework bookkeeping.
        native["messages"] = messages[turn_start:]
    return {"langgraph": json_value(native)}


def _adapted_output(adapted: Any) -> tuple[str, Any]:
    if isinstance(adapted, str):
        return adapted, adapted
    return _visible_value(adapted), json_value(adapted)


def _mapping_output(result: Mapping[str, Any]) -> tuple[str, Any]:
    structured = json_value(
        {
            key: value
            for key, value in result.items()
            if key not in {"messages", _TURN_START_KEY, _SESSION_STATE_KEY}
        }
    )
    public_result = structured or None
    messages = result.get("messages")
    if isinstance(messages, (list, tuple)) and messages:
        content = _message_text(messages[-1])
        if content:
            return content, public_result
        if public_result is not None:
            return _visible_value(public_result), public_result
        # A reasoning-only AI message is not a customer response. Returning its
        # object representation could leak provider internals and would bypass
        # the neutral runtime's empty-output protection.
        return "", None
    value = result.get("value")
    if value is None:
        return _visible_value(result), json_value(result)
    return (value if isinstance(value, str) else _visible_value(value)), public_result


def _managed_graph_user_state(state: Mapping[str, Any]) -> dict[str, Any]:
    nested = state.get(_SESSION_STATE_KEY)
    if isinstance(nested, Mapping):
        return dict(nested)
    return {
        str(key): value
        for key, value in state.items()
        if key not in _GRAPH_RUNTIME_KEYS
    }


def _managed_graph_internal_state(
    state: Mapping[str, Any], user_state: Mapping[str, Any]
) -> dict[str, Any]:
    internal = {
        key: value for key, value in state.items() if key in _GRAPH_RUNTIME_KEYS
    }
    internal[_SESSION_STATE_KEY] = dict(user_state)
    return internal


def _visible_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(json_value(value), ensure_ascii=False)


def _message_text(message: Any) -> str:
    message_type = getattr(message, "type", None)
    if message_type not in {None, "ai", "AIMessage", "AIMessageChunk"}:
        return ""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if not isinstance(content, (list, tuple)):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") in {"text", "output_text"} and isinstance(
            block.get("text"), str
        ):
            parts.append(block["text"])
    return "".join(parts)


async def _reference_safe_message(
    message: Any,
    *,
    store: AssetStore | None,
    scope: AssetScope,
) -> dict[str, Any]:
    """Reduce a native message to replayable fields with no inline media."""

    native = json_value(message)
    if not isinstance(native, Mapping):
        return {"type": "ai", "content": None}
    content = native.get("content")
    if isinstance(content, list):
        content = await _reference_safe_blocks(
            content, store=store, scope=scope
        )
    elif not isinstance(content, str):
        content = None
    safe = {
        "type": native.get("type", "ai"),
        "content": content,
    }
    for key in ("tool_calls", "tool_call_id", "name"):
        if native.get(key) is not None:
            safe[key] = native[key]
    return safe


async def _reference_safe_blocks(
    content: Sequence[Any],
    *,
    store: AssetStore | None,
    scope: AssetScope,
) -> list[dict[str, Any]]:
    """Keep portable blocks and stage inline provider output when possible."""

    blocks: list[dict[str, Any]] = []
    for value in content:
        if isinstance(value, str):
            blocks.append({"type": "text", "text": value})
            continue
        if not isinstance(value, Mapping):
            continue
        reference = _safe_reference_block(value)
        if reference is not None:
            blocks.append(reference)
            continue
        inline = _inline_asset(value)
        if inline is not None and store is not None:
            kind, media_type, payload = inline
            metadata, inspected_type = inspect_asset(payload, media_type)
            record = await store.save(
                scope=scope,
                media_type=inspected_type,
                chunks=_single_asset_chunk(payload),
                metadata=metadata,
            )
            blocks.append(
                {
                    "type": kind,
                    "assetId": record.asset_id,
                    "mediaType": record.media_type,
                    "sizeBytes": record.size_bytes,
                }
            )
            continue
        ordinary = _safe_ordinary_block(value)
        if ordinary is not None:
            blocks.append(ordinary)
    return blocks


async def _single_asset_chunk(payload: bytes) -> AsyncIterator[bytes]:
    """Expose decoded model output through the store's streaming contract."""

    yield payload


def _safe_reference_block(value: Mapping[str, Any]) -> dict[str, Any] | None:
    """Canonicalize one Harnest reference and discard provider-only fields."""

    asset_id = value.get("assetId") or value.get("asset_id")
    if not isinstance(asset_id, str):
        return None
    kind = str(value.get("type") or "asset")
    if kind not in {"asset", "image", "audio", "video", "file"}:
        return None
    result: dict[str, Any] = {"type": kind, "assetId": asset_id}
    aliases = {
        "mediaType": ("mediaType", "media_type", "mime_type"),
        "sizeBytes": ("sizeBytes", "size_bytes"),
        "width": ("width",),
        "height": ("height",),
        "durationSeconds": ("durationSeconds", "duration_seconds"),
        "frameCount": ("frameCount", "frame_count"),
        "pageCount": ("pageCount", "page_count"),
        "sampleRateHz": ("sampleRateHz", "sample_rate_hz"),
        "channels": ("channels", "channel_count"),
    }
    for target, sources in aliases.items():
        selected = next(
            (
                value.get(source)
                for source in sources
                if value.get(source) is not None
            ),
            None,
        )
        if selected is not None:
            result[target] = selected
    return result


def _inline_asset(value: Mapping[str, Any]) -> tuple[str, str, bytes] | None:
    """Decode supported inline provider media without accepting remote URLs."""

    kind = _inline_asset_kind(value.get("type"))
    if kind is None:
        return None
    encoded, media_type = _inline_asset_encoding(value)
    if not isinstance(encoded, str) or not isinstance(media_type, str):
        return None
    payload = _decoded_base64(encoded)
    return None if payload is None else (kind, media_type, payload)


def _inline_asset_kind(value: Any) -> str | None:
    """Normalize the legacy image-url spelling without accepting other blocks."""

    kind = str(value or "")
    if kind == "image_url":
        return "image"
    return kind if kind in {"image", "audio", "video", "file"} else None


def _inline_asset_encoding(value: Mapping[str, Any]) -> tuple[Any, Any]:
    """Extract encoded bytes and MIME type from supported provider shapes."""

    source = value.get("source")
    encoded = value.get("base64")
    media_type = value.get("mime_type") or value.get("media_type")
    if isinstance(source, Mapping) and source.get("type") == "base64":
        encoded = source.get("data")
        media_type = source.get("media_type") or media_type
    url = _content_url(value)
    if isinstance(url, str) and url.startswith("data:"):
        media_type, encoded = _data_url_parts(url)
    return encoded, media_type


def _decoded_base64(value: str) -> bytes | None:
    """Decode strict base64 while treating malformed provider output as unsafe."""

    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None


def _content_url(value: Mapping[str, Any]) -> Any:
    """Read direct and legacy nested URL fields without following them."""

    nested = value.get("image_url")
    if isinstance(nested, Mapping):
        return nested.get("url")
    return value.get("url")


def _data_url_parts(value: str) -> tuple[str | None, str | None]:
    """Split a base64 data URL while rejecting non-base64 payloads."""

    header, separator, encoded = value.partition(",")
    if not separator or not header.endswith(";base64"):
        return None, None
    media_type = header.removeprefix("data:").removesuffix(";base64")
    return media_type or None, encoded


def _safe_ordinary_block(value: Mapping[str, Any]) -> dict[str, Any] | None:
    """Retain portable text/data while dropping opaque provider annotations."""

    kind = value.get("type")
    if kind in {"text", "output_text"} and isinstance(value.get("text"), str):
        return {"type": "text", "text": value["text"]}
    if kind == "data" and "value" in value:
        try:
            json.dumps(value["value"], allow_nan=False)
        except (TypeError, ValueError, OverflowError):
            return None
        return {"type": "data", "value": value["value"]}
    return None


def _langgraph_session_messages(record: SessionRecord) -> list[SessionMessage]:
    """Project stored messages without exposing native inline/provider data."""

    native_messages = record.state.get("messages")
    if not isinstance(native_messages, (list, tuple)):
        return []
    messages: list[SessionMessage] = []
    for index, message in enumerate(native_messages):
        native = json_value(message)
        if not isinstance(native, Mapping):
            continue
        role = _langgraph_message_role(native)
        messages.append(
            SessionMessage(
                # Native IDs can encode provider-side references. Public
                # transcript identity is stable within the Harnest session.
                id=f"{record.id}:{index}",
                role=role,
                content=_langgraph_message_content(native, role=role),
                created_at=_langgraph_message_created_at(native),
                metadata={"langgraph": _langgraph_message_metadata(native)},
            )
        )
    return messages


def _langgraph_message_role(message: Mapping[str, Any]) -> str:
    """Map LangChain message types onto the portable transcript roles."""

    message_type = message.get("type")
    return {
        "human": "user",
        "ai": "assistant",
        "tool": "tool",
        "system": "system",
    }.get(str(message_type), "assistant")


def _langgraph_message_content(message: Mapping[str, Any], *, role: str) -> Any:
    """Expose text compatibly or ordered portable reference-only blocks."""

    content = message.get("content")
    if isinstance(content, str) or content is None:
        return content
    if not isinstance(content, list):
        return content
    blocks = _portable_history_blocks(content)
    if _only_text_blocks(blocks):
        return "".join(str(block["text"]) for block in blocks)
    if blocks:
        return blocks
    return None if role != "tool" else []


def _portable_history_blocks(content: Sequence[Any]) -> list[dict[str, Any]]:
    """Drop native annotations and retain portable ordered content blocks."""

    blocks: list[dict[str, Any]] = []
    for value in content:
        if not isinstance(value, Mapping):
            continue
        safe = _safe_reference_block(value) or _safe_ordinary_block(value)
        if safe is not None:
            blocks.append(safe)
    return blocks


def _only_text_blocks(blocks: Sequence[Mapping[str, Any]]) -> bool:
    """Preserve the historical string shape for text-only messages."""

    return bool(blocks) and all(block.get("type") == "text" for block in blocks)


def _langgraph_message_metadata(message: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only low-risk framework identity outside portable message content."""

    metadata = {"type": str(message.get("type") or "ai")}
    if isinstance(message.get("name"), str):
        metadata["name"] = message["name"]
    return metadata


def _langgraph_message_created_at(message: Mapping[str, Any]) -> str | None:
    """Use a native textual timestamp when a message implementation supplies one."""

    for key in ("createdAt", "created_at", "timestamp"):
        value = message.get(key)
        if isinstance(value, str):
            return value
    return None


def _message_tool_events(message: Any) -> list[dict[str, Any]]:
    events = []
    for call in getattr(message, "tool_calls", ()) or ():
        if isinstance(call, Mapping):
            events.append(
                {
                    "type": "tool_call",
                    "id": call.get("id"),
                    "name": call.get("name"),
                    "arguments": json_value(call.get("args", {})),
                }
            )
    if getattr(message, "type", None) == "tool":
        events.append(
            {
                "type": "tool_result",
                "id": getattr(message, "tool_call_id", None),
                "name": getattr(message, "name", None),
                "result": json_value(getattr(message, "content", None)),
            }
        )
    return events


def _tool_events(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, Mapping):
        return []
    messages = result.get("messages")
    if not isinstance(messages, (list, tuple)):
        return []
    events = []
    for message in messages:
        events.extend(_message_tool_events(message))
    return events


def _result_events(
    result: Any,
    text_value: str,
    public_result: Any,
    output_policy: OutputPolicy,
    *,
    turn_start: int,
) -> list[dict[str, Any]]:
    """Project one invocation using the same policy as the streaming boundary."""

    if output_policy.subagent_messages == "include":
        events = _included_message_events(result, turn_start=turn_start)
    else:
        events = _tool_events(result)
    if text_value and not _events_end_with_text(events, text_value):
        events.append({"type": "message", "role": "assistant", "text": text_value})
    if public_result is not None:
        events.append(
            {
                "type": "graph_output",
                "output": public_result,
                "result": public_result,
            }
        )
    return events


def _included_message_events(result: Any, *, turn_start: int) -> list[dict[str, Any]]:
    """Expose current-turn AI narration without replaying prior session messages."""

    if not isinstance(result, Mapping):
        return []
    messages = result.get("messages")
    if not isinstance(messages, (list, tuple)):
        return []
    events: list[dict[str, Any]] = []
    for message in messages[turn_start:]:
        content = _message_text(message)
        if content:
            events.append({"type": "message", "role": "assistant", "text": content})
        events.extend(_message_tool_events(message))
    return events


def _events_end_with_text(events: Sequence[Mapping[str, Any]], text: str) -> bool:
    """Avoid duplicating a canonical reply already present in included output."""

    for event in reversed(events):
        if event.get("type") == "message":
            return event.get("text") == text
    return False


def _result_text(events: Sequence[Mapping[str, Any]], *, fallback: str) -> str:
    """Build response text only from messages selected for public output."""

    text = "".join(
        str(event.get("text", ""))
        for event in events
        if event.get("type") == "message"
    )
    return text or fallback


def _message_count(graph_input: Any) -> int:
    """Locate the current turn without assuming an advanced adapter shape."""

    if not isinstance(graph_input, Mapping):
        return 0
    messages = graph_input.get("messages")
    return len(messages) if isinstance(messages, (list, tuple)) else 0


def _event_identity(event: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        event.get("type"),
        event.get("id"),
        event.get("name"),
        json.dumps(json_value(event), sort_keys=True, ensure_ascii=False),
    )


def _stream_item(item: Any) -> tuple[str, Any]:
    if isinstance(item, tuple) and len(item) == 2 and item[0] in {
        "messages",
        "values",
    }:
        return item[0], item[1]
    # A target may ignore the requested multi-mode stream and yield values only.
    return "values", item


__all__ = ["LangGraphRuntimeDriver"]
