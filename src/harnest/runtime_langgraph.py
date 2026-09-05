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
from contextlib import aclosing, asynccontextmanager, contextmanager
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import BaseModel

from ._exception_notes import add_exception_note
from ._json import json_value
from .approval import ApprovalPolicy
from .application import CompiledApplication
from .assets import AssetScope, AssetStore, AssetURLStorage
from .asset_inspection import inspect_asset
from .backends.langgraph import (
    ManagedGraphPlan,
    _AGENT_PRINCIPAL_PROJECTION_COMPLETE,
    _graph_agent_principal_projection_complete,
)
from .checkpoint import CheckpointStore, HarnestStore, RunScope
from .checkpoint_langgraph import managed_run_config
from .client_tool import current_transient_media
from .content import AssetRef, Audio, Data, File, Image, Text, Video
from .context_session import current_session_lease, invocation_session_context
from .durable import NativeDurableSuspended, NativeResumeInput
from .graph import _model_input_text
from .model_lifecycle import close_litellm_lifecycles
from .mcp import (
    _invoke_governed_mcp_call,
    _mcp_result_failed,
    _validate_approval_tools,
    _validate_mcp_permission_tools,
)
from .mcp_context import (
    MCPToolCallError,
    _activate_mcp_context,
    _managed_mcp_tool,
    _mark_governed_mcp_operation,
)
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
from .runtime_session import durable_completion_deferred
from .output import AgentMetadata, OutputPolicy, TokenUsage, _reported_token_usage
from .session import InMemorySessionStore, SessionLease, SessionStore
from .structured import validate_runtime_output
from .transient_media import (
    TransientMediaAccess,
    TransientMediaLease,
    is_transient_media_placeholder,
    matching_transient_leases,
    sanitize_transient_media,
    transient_media_lease_id,
    transient_media_placeholders,
)
from .stored_media import (
    sanitize_stored_media,
    stored_media_reference,
    stored_media_references,
    stage_stored_media,
)


_TURN_START_KEY = "_harnest_turn_start"
_SESSION_STATE_KEY = "_harnest_state"
_GRAPH_RUNTIME_KEYS = frozenset(
    {"messages", "value", "route", _TURN_START_KEY, _SESSION_STATE_KEY}
)
_MODEL_ASSET_SCOPE: contextvars.ContextVar[AssetScope | None] = (
    contextvars.ContextVar("harnest_langgraph_asset_scope", default=None)
)
_MCP_NATIVE_CALL_SCOPE: contextvars.ContextVar[
    tuple[object, Any, Any, Mapping[str, Any]] | None
] = contextvars.ContextVar("harnest_langgraph_mcp_native_call", default=None)
_CONTENT_PART_TYPES = (Text, Image, Audio, Video, File, Data, AssetRef)
_NO_ORDINARY_CONTENT = object()


@dataclass(slots=True)
class _StreamState:
    final_state: Any = None
    text: str = ""
    tools: set[tuple[Any, ...]] = field(default_factory=set)
    active_agents: set[str] = field(default_factory=set)
    active_task_counts: dict[str, int] = field(default_factory=dict)
    metadata: set[tuple[Any, ...]] = field(default_factory=set)


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
        asset_stores: Mapping[str, AssetStore] | None = None,
    ) -> None:
        plan = _runtime_plan(application, recursion_limit)
        card_value = dict(card or {})
        self._application = application
        self._plan = plan
        self._target = None if plan is not None else application.target
        self._mcp_clients: list[Any] = []
        self._mcp_lifecycles: list[_MCPClientLifecycleBinding] = []
        self._mcp_context_clients: dict[str, dict[str, Any]] = {}
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
        stores = dict(getattr(application, "asset_stores", {}))
        stores.update(asset_stores or {})
        if configured_assets is not None:
            stores["default"] = configured_assets
        self._asset_stores = stores
        self._asset_store = stores.get("default")
        self._closed = False

    @property
    def info(self) -> AgentInfo:
        return self._info

    @property
    def session_context_store(self) -> SessionStore:
        """Expose the portable authority so outer lifecycle hooks reuse its lease."""

        return self._session_store

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
        native_resume = _native_resume(request.input)
        async with _invocation_session(
            self._session_store, request, session_id
        ) as session:
            scope = AssetScope(user_id=request.user_id, session_id=session_id)
            config = self._execution_config(request, session_id)
            if native_resume is None:
                await self._apply_request_delta(session, request)
                graph_input = await _graph_input(
                    self._application,
                    request.input,
                    session.record.state,
                    stores=self._asset_stores,
                    scope=scope,
                )
                turn_start = _message_count(graph_input)
                await self._begin_checkpoint(request, session_id)
            else:
                # The checkpoint is the suspended invocation's input. Applying
                # the original request again would duplicate the user turn.
                turn_start = _message_count(session.record.state)
                graph_input = _resume_command(native_resume, config)
            target = await self._target_for_run()
            scope_token = _MODEL_ASSET_SCOPE.set(scope)
            try:
                with self._mcp_invocation_scope():
                    result = await _target_invoke(target, graph_input, config)
                await self._store_result(session, result, scope=scope)
                if _has_native_interrupt(result):
                    # Pregel returns interrupts only after its sync checkpoint
                    # write, so another replica can safely consume the signal.
                    raise NativeDurableSuspended
            except NativeDurableSuspended:
                raise
            except BaseException:
                await self._finish_checkpoint(request, session_id, "failed")
                raise
            finally:
                _MODEL_ASSET_SCOPE.reset(scope_token)
            await self._finish_checkpoint(request, session_id, "completed")

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
        native_resume = _native_resume(request.input)
        async with _invocation_session(
            self._session_store, request, session_id
        ) as session:
            scope = AssetScope(user_id=request.user_id, session_id=session_id)
            config = self._execution_config(request, session_id)
            if native_resume is None:
                await self._apply_request_delta(session, request)
                graph_input = await _graph_input(
                    self._application,
                    request.input,
                    session.record.state,
                    stores=self._asset_stores,
                    scope=scope,
                )
                turn_start = _message_count(graph_input)
                await self._begin_checkpoint(request, session_id)
            else:
                # A resume consumes the persisted Pregel checkpoint directly;
                # portable session state remains the committed public history.
                turn_start = _message_count(session.record.state)
                graph_input = _resume_command(native_resume, config)
            state = _StreamState()
            scope_token = _MODEL_ASSET_SCOPE.set(scope)
            try:
                with self._mcp_invocation_scope():
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
                if _has_native_interrupt(state.final_state):
                    raise NativeDurableSuspended
            except NativeDurableSuspended:
                raise
            except BaseException:
                await self._finish_checkpoint(request, session_id, "failed")
                raise
            finally:
                _MODEL_ASSET_SCOPE.reset(scope_token)
            await self._finish_checkpoint(request, session_id, "completed")

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
            runtime_plan = _plan_with_asset_middleware(
                plan,
                self._asset_stores,
                structured_input=self._application.input_schema is not None,
            )
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
                add_exception_note(
                    error,
                    "MCP startup cleanup also failed with "
                    f"{type(cleanup_error).__name__}",
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
            discovered = await _discover_configured_mcp_tools(
                client, server_name, configured, resources=self._mcp_clients,
            )
            selected = _filtered_mcp_tools(
                discovered, server_name=server_name, allowed=configured.tool_filter
            )
            _validate_mcp_approval(selected, server_name, configured)
            _apply_mcp_permissions(selected, server_name, configured)
            public_name = configured.identity or server_name
            self._register_mcp_context_tools(public_name, server_name, selected)
            tools.extend(selected)
        return tools

    def _register_mcp_context_tools(
        self,
        public_name: str,
        capability_id: str,
        tools: Sequence[Any],
    ) -> None:
        """Give native and context dispatch the same governed MCP marker."""

        if public_name in self._mcp_context_clients:
            raise ValueError(
                f"duplicate LangGraph MCP public name {public_name!r}"
            )
        governed: dict[str, Any] = {}
        for tool in tools:
            native_name = str(getattr(tool, "name", ""))
            public_tool_name = _remote_mcp_tool_name(native_name, capability_id)
            if public_tool_name in governed:
                raise ValueError(
                    "duplicate LangGraph MCP public tool name "
                    f"{public_tool_name!r}"
                )
            marker = _langgraph_mcp_marker(capability_id, public_tool_name, tool)
            governed[public_tool_name] = marker
        self._mcp_context_clients[public_name] = governed

    @contextmanager
    def _mcp_invocation_scope(self):
        """Bind MCP authority only while an outer managed context is active."""

        if not self._mcp_context_clients or not _has_agent_context():
            # Direct backend tests and unsupported advanced execution have no
            # capability context; marker invocation still fails closed there.
            yield
            return
        with _activate_mcp_context(
            self._mcp_context_clients, self._application.extensions
        ):
            yield

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
        self._mcp_context_clients.clear()
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
        """Resolve an existing session without reacquiring its outer lease."""

        self._ensure_open()
        user_id = request.user_id
        _require_identifier(user_id, "request.user_id")
        requested_id = request.session_id
        if requested_id is not None:
            _require_identifier(requested_id, "request.session_id")
            active = current_session_lease(
                store=self._session_store,
                user_id=user_id,
                session_id=requested_id,
                invocation_id=request.invocation_id,
            )
            stored = (
                active.record
                if active is not None
                else await self._session_store.get(
                    user_id=user_id, session_id=requested_id
                )
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

    async def _finish_checkpoint(
        self, request: InvocationRequest, session_id: str, status: str
    ) -> None:
        """Release portable run ownership after the graph becomes terminal."""

        if status == "completed" and durable_completion_deferred(
            request.invocation_id
        ):
            # The outer storage wrapper persists lifecycle-final metadata before
            # publishing terminal completion to another replica.
            return
        store = self._portable_checkpoints()
        if store is None:
            return
        scope = self._run_scope(request, session_id)
        record = await store.get_run(scope=scope)
        if record is not None and record.status == "running":
            await store.transition(
                scope=scope,
                expected_status="running",
                status=status,
            )

    def _portable_checkpoints(self) -> CheckpointStore | None:
        """Use portable checkpoints only when Harnest owns the native adapter."""

        provider = self._application.checkpointer
        return provider if isinstance(provider, HarnestStore) else None

    def _run_scope(
        self, request: InvocationRequest, session_id: str
    ) -> RunScope:
        return RunScope(
            self._application.name,
            request.user_id,
            session_id,
            request.invocation_id,
        )

    def _execution_config(
        self, request: InvocationRequest, session_id: str
    ) -> dict[str, Any]:
        """Isolate native checkpoint threads while SessionStore carries history."""

        # A thread represents one invocation. SessionStore remains the
        # committed multi-turn authority across these isolated runs.
        return {
            **managed_run_config(self._run_scope(request, session_id)),
            "recursion_limit": self._recursion_limit,
        }

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("LangGraph runtime driver is closed")

    def _public_session(self, record: SessionRecord) -> SessionRecord:
        """Separate portable state from lossless framework-owned session data."""

        metadata = {
            **dict(record.metadata),
            "langgraph": sanitize_stored_media(json_value(record.state)),
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


def _plan_with_asset_middleware(
    plan: Any,
    stores: Mapping[str, AssetStore] | AssetStore | None,
    *,
    structured_input: bool = False,
) -> Any:
    """Attach byte materialization as the innermost managed-model boundary."""

    if not stores and not structured_input and not _plan_has_typed_tools(plan):
        return plan
    middleware = tuple(getattr(plan, "middleware", ()))
    # Appending keeps extension middleware outside this adapter so hooks and
    # telemetry observe references, while only the provider sees byte content.
    return replace(
        plan,
        middleware=(*middleware, _langgraph_asset_middleware(stores)),
    )


def _plan_has_typed_tools(plan: Any) -> bool:
    """Detect tools whose Pydantic output may declare transient media."""

    definition = getattr(plan, "definition", None)
    if definition is not None:
        return any(
            getattr(tool, "__harnest_output_schema__", None) is not None
            for tool in getattr(definition, "tools", ())
        )
    graph = getattr(plan, "graph", None)
    nodes = getattr(graph, "nodes", None)
    if not isinstance(nodes, Mapping):
        return False
    return any(
        _plan_node_has_typed_tools(node) for node in nodes.values()
    )


def _plan_node_has_typed_tools(node: Any) -> bool:
    """Inspect nested portable graph definitions without materializing them."""

    if any(
        getattr(tool, "__harnest_output_schema__", None) is not None
        for tool in getattr(node, "tools", ())
    ):
        return True
    nodes = getattr(node, "nodes", None)
    return isinstance(nodes, Mapping) and any(
        _plan_node_has_typed_tools(child) for child in nodes.values()
    )


def _langgraph_asset_middleware(
    stores: Mapping[str, AssetStore] | AssetStore | None,
) -> Any:
    """Build middleware that swaps scoped references only around model I/O."""

    from langchain.agents.middleware import AgentMiddleware

    if stores is None:
        storage_registry: dict[str, AssetStore] = {}
    elif isinstance(stores, AssetStore):
        storage_registry = {"default": stores}
    else:
        storage_registry = dict(stores)
    default_store = storage_registry.get("default")

    class AssetBoundaryMiddleware(AgentMiddleware):
        def __init__(self) -> None:
            self._transient_tools: dict[
                tuple[str, str, str, str],
                tuple[TransientMediaAccess, tuple[str, ...]],
            ] = {}

        async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
            """Bind safe tool placeholders to their private native tool-call ID."""

            result = await handler(request)
            access = current_transient_media()
            content = getattr(result, "content", None)
            sanitized, _, _ = _message_transient_value(content)
            leases = matching_transient_leases(
                transient_media_placeholders(sanitized),
                access.pending() if access is not None else (),
            )
            tool_call_id = getattr(result, "tool_call_id", None)
            if access is not None and leases and isinstance(tool_call_id, str):
                self._transient_tools[_langgraph_tool_key(access, tool_call_id)] = (
                    access,
                    tuple(lease.lease_id for lease in leases),
                )
            return result

        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            scope = _MODEL_ASSET_SCOPE.get()
            if scope is None:
                if _messages_have_asset_references(request.messages):
                    raise RuntimeError("asset model call requires invocation scope")
            access = current_transient_media()
            transient_by_message = _langgraph_pending_messages(
                request.messages, access, self._transient_tools
            )
            messages: list[Any] = []
            lease_ids: list[str] = []
            for index, message in enumerate(request.messages):
                projected, discovered = await _materialize_model_message(
                    message,
                    stores=storage_registry,
                    scope=scope,
                    transient_leases=transient_by_message.get(index, ()),
                )
                messages.append(projected)
                lease_ids.extend(discovered)
            # Commit follows success only. Invocation cleanup owns terminal
            # failure, leaving framework retries able to reuse identical bytes.
            response = await handler(request.override(messages=messages))
            if access is not None and lease_ids:
                access.commit(lease_ids)
                _forget_langgraph_tools(self._transient_tools, lease_ids)
            if default_store is None or scope is None:
                return response
            return await _reference_safe_model_response(
                response, store=default_store, scope=scope
            )

        def wrap_model_call(self, request: Any, handler: Any) -> Any:
            if _messages_have_asset_references(
                request.messages
            ) or _messages_have_transient_markers(request.messages):
                raise RuntimeError(
                    "media-backed LangGraph calls require asynchronous invocation"
                )
            return handler(request)

    return AssetBoundaryMiddleware()


def _messages_have_asset_references(messages: Sequence[Any]) -> bool:
    """Detect portable references without inspecting or reflecting their IDs."""

    return any(
        _content_has_asset_reference(getattr(message, "content", None))
        for message in messages
    )


def _messages_have_transient_markers(messages: Sequence[Any]) -> bool:
    """Detect legacy markers or safe placeholders requiring async lowering."""

    for message in messages:
        value, lease_ids, _ = _message_transient_value(
            getattr(message, "content", None)
        )
        if lease_ids or transient_media_placeholders(value):
            return True
    return False


def _content_has_asset_reference(content: Any) -> bool:
    """Recognize reference blocks in one native message content value."""

    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (TypeError, ValueError):
            return False
    if isinstance(content, Mapping):
        if isinstance(content.get("assetId") or content.get("asset_id"), str):
            return True
        return any(_content_has_asset_reference(item) for item in content.values())
    if isinstance(content, (list, tuple)):
        return any(_content_has_asset_reference(item) for item in content)
    return False


def _message_transient_value(content: Any) -> tuple[Any, tuple[str, ...], bool]:
    """Sanitize nested markers, including LangChain's JSON tool-result text."""

    value = content
    was_json = False
    if isinstance(content, str):
        try:
            value = json.loads(content)
        except (TypeError, ValueError):
            return content, (), False
        was_json = True
    sanitized, lease_ids = sanitize_transient_media(value)
    return sanitized, lease_ids, was_json


def _langgraph_skeleton_blocks(value: Any) -> list[dict[str, Any]]:
    """Render marker-free tool JSON without provider-specific media syntax."""

    return [
        {
            "type": "text",
            "text": json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        }
    ]


def _langgraph_pending_messages(
    messages: Sequence[Any],
    access: TransientMediaAccess | None,
    bindings: Mapping[
        tuple[str, str, str, str],
        tuple[TransientMediaAccess, tuple[str, ...]],
    ] | None = None,
) -> dict[int, tuple[TransientMediaLease, ...]]:
    """Bind pending bytes to newest matching messages in the current branch."""

    remaining = list(access.pending()) if access is not None else []
    matched: dict[int, tuple[TransientMediaLease, ...]] = {}
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        content = getattr(message, "content", None)
        sanitized, _, _ = _message_transient_value(content)
        candidates = _langgraph_bound_leases(message, access, bindings)
        leases = matching_transient_leases(
            transient_media_placeholders(sanitized), candidates or remaining
        )
        if not leases:
            continue
        matched[index] = leases
        selected = {lease.lease_id for lease in leases}
        remaining = [lease for lease in remaining if lease.lease_id not in selected]
    return matched


def _langgraph_tool_key(
    access: TransientMediaAccess, tool_call_id: str
) -> tuple[str, str, str, str]:
    """Build a private invocation and native-tool correlation key."""

    scope = access.scope
    return scope.user_id, scope.session_id, scope.call_id, tool_call_id


def _langgraph_bound_leases(
    message: Any,
    access: TransientMediaAccess | None,
    bindings: Mapping[
        tuple[str, str, str, str],
        tuple[TransientMediaAccess, tuple[str, ...]],
    ] | None,
) -> tuple[TransientMediaLease, ...]:
    """Resolve the exact tool binding across LangGraph task boundaries."""

    tool_call_id = getattr(message, "tool_call_id", None)
    if access is None or not isinstance(tool_call_id, str) or not bindings:
        return ()
    binding = bindings.get(_langgraph_tool_key(access, tool_call_id))
    if binding is None:
        return ()
    owner, lease_ids = binding
    return tuple(
        lease
        for lease_id in lease_ids
        if (lease := owner.peek(lease_id)) is not None
    )


def _forget_langgraph_tools(
    bindings: dict[
        tuple[str, str, str, str],
        tuple[TransientMediaAccess, tuple[str, ...]],
    ],
    lease_ids: Sequence[str],
) -> None:
    """Remove successful tool correlations while preserving retry bindings."""

    committed = set(lease_ids)
    stale = [
        key
        for key, (_, values) in bindings.items()
        if committed.intersection(values)
    ]
    for key in stale:
        bindings.pop(key, None)


def _langgraph_transient_block(lease: TransientMediaLease) -> dict[str, Any]:
    """Translate one lease into LangChain's provider-neutral media block."""

    return {
        "type": lease.kind,
        "base64": base64.b64encode(lease.data).decode("ascii"),
        "mime_type": lease.media_type,
    }


async def _materialize_model_message(
    message: Any,
    *,
    stores: Mapping[str, AssetStore],
    scope: AssetScope | None,
    transient_leases: Sequence[TransientMediaLease] = (),
) -> tuple[Any, list[str]]:
    """Clone a message with provider-ready bytes for this model call only."""

    content = getattr(message, "content", None)
    sanitized, discovered, _ = _message_transient_value(content)
    durable = stored_media_references(sanitized)
    direct_assets = _direct_asset_blocks(content)
    if not direct_assets and not discovered and not durable and not transient_leases:
        return message, []
    blocks = await _langgraph_base_blocks(
        content, sanitized, direct_assets, stores, scope
    )
    blocks.extend(_langgraph_transient_block(lease) for lease in transient_leases)
    blocks.extend(await _langgraph_durable_blocks(durable, stores, scope))
    copier = getattr(message, "model_copy", None)
    if not callable(copier):
        raise TypeError("LangChain messages must support model_copy")
    return copier(update={"content": blocks}), [
        lease.lease_id for lease in transient_leases
    ]


def _direct_asset_blocks(content: Any) -> bool:
    """Detect a native content-block list containing direct asset references."""

    return isinstance(content, (list, tuple)) and any(
        isinstance(block, Mapping)
        and isinstance(block.get("assetId") or block.get("asset_id"), str)
        for block in content
    )


async def _langgraph_base_blocks(
    content: Any,
    sanitized: Any,
    direct_assets: bool,
    stores: Mapping[str, AssetStore],
    scope: AssetScope | None,
) -> list[Any]:
    """Build the detached ordinary or reference-backed message blocks."""

    if not direct_assets:
        return _langgraph_skeleton_blocks(sanitize_stored_media(sanitized))
    if not stores or scope is None:
        raise RuntimeError("asset model call requires invocation scope")
    return [
        await _materialize_model_block(block, stores=stores, scope=scope)
        for block in content
    ]


async def _langgraph_durable_blocks(
    references: tuple[Any, ...],
    stores: Mapping[str, AssetStore],
    scope: AssetScope | None,
) -> list[dict[str, Any]]:
    """Resolve nested durable tool references for one model attempt."""

    if not references:
        return []
    if scope is None:
        raise RuntimeError("asset model call requires invocation scope")
    return [
        await _langgraph_stored_block(item, stores, scope) for item in references
    ]


async def _materialize_model_block(
    block: Any, *, stores: Mapping[str, AssetStore], scope: AssetScope
) -> Any:
    """Resolve one opaque asset into a standard LangChain content block."""

    if not isinstance(block, Mapping):
        return block
    asset_id = block.get("assetId") or block.get("asset_id")
    if not isinstance(asset_id, str):
        return dict(block)
    durable = stored_media_reference(block)
    if durable is not None:
        return await _langgraph_stored_block(durable, stores, scope)
    store_name = block.get("store", "default")
    store = stores.get(store_name) if isinstance(store_name, str) else None
    if store is None:
        raise ValueError("content asset storage is unavailable")
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


async def _langgraph_stored_block(
    reference: Any,
    stores: Mapping[str, AssetStore],
    scope: AssetScope,
) -> dict[str, Any]:
    """Generate one explicit temporary URL immediately before a model call."""

    storage = stores.get(reference.store)
    if storage is None or not isinstance(storage, AssetURLStorage):
        raise RuntimeError("declared asset storage cannot generate model URLs")
    record = await storage.stat(scope=scope, asset_id=reference.asset_id)
    if record is None:
        raise ValueError("content asset is unavailable")
    url = await storage.signed_url(
        scope=scope,
        asset_id=reference.asset_id,
        expires_in=reference.expires_in,
    )
    if not isinstance(url, str) or not url.startswith("https://"):
        raise RuntimeError("declared asset storage returned an invalid model URL")
    return {
        "type": _provider_content_kind(reference.kind, record.media_type),
        "url": url,
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
    """Build transport metadata including principal projection coverage."""

    projection_complete = application.mode == "managed"
    if projection_complete and application.kind == "graph":
        target = application.target
        projection_complete = (
            _graph_agent_principal_projection_complete(target.graph)
            if isinstance(target, ManagedGraphPlan)
            else getattr(
                target, _AGENT_PRINCIPAL_PROJECTION_COMPLETE, False
            )
            is True
        )
    return AgentInfo(
        id=application.name,
        name=str(card.get("name") or application.name),
        description=str(card.get("description") or ""),
        card=card,
        framework="langgraph",
        mode=application.mode,
        lifecycle_coverage=application.lifecycle_coverage.report(),
        input_schema=application.input_schema,
        output_schema=application.output_schema,
        agent_principal_projection_complete=projection_complete,
    )


def _native_resume(value: Any) -> NativeResumeInput | None:
    """Separate compiler-owned resume input from ordinary authored input."""

    return value if isinstance(value, NativeResumeInput) else None


def _resume_command(
    resume: NativeResumeInput, config: Mapping[str, Any]
) -> Any:
    """Build a Command only after its persisted thread matches request ownership."""

    artifact = resume.artifact
    if artifact.framework != "langgraph":
        raise ValueError("LangGraph runtime cannot resume another framework")
    configurable = config.get("configurable")
    thread_id = (
        configurable.get("thread_id")
        if isinstance(configurable, Mapping)
        else None
    )
    if artifact.native_invocation_id != thread_id:
        # The persisted artifact is private, but checking it again prevents a
        # corrupt record from crossing application, principal, or run scope.
        raise ValueError("LangGraph durable resume thread does not match request")
    from langgraph.types import Command

    return Command(resume=resume.value)


def _durability_options(method: Any) -> dict[str, str]:
    """Request synchronous checkpoints when the target supports that contract."""

    try:
        parameters = inspect.signature(method).parameters.values()
    except (TypeError, ValueError):
        return {}
    supported = any(
        parameter.name == "durability"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )
    # Older compatible targets do not expose durability. Pregel 1.2 does, and
    # sync ordering is what makes a returned interrupt safe for another replica.
    return {"durability": "sync"} if supported else {}


async def _target_invoke(
    target: Any, graph_input: Any, config: Mapping[str, Any]
) -> Any:
    """Invoke one target with the strongest checkpoint durability it supports."""

    return await target.ainvoke(
        graph_input,
        config=config,
        **_durability_options(target.ainvoke),
    )


def _has_native_interrupt(result: Any) -> bool:
    """Recognize Pregel's persisted top-level interruption result."""

    if not isinstance(result, Mapping):
        return False
    interrupts = result.get("__interrupt__")
    return isinstance(interrupts, (list, tuple)) and bool(interrupts)


async def _target_stream(
    target: Any,
    graph_input: Any,
    config: Mapping[str, Any],
    state: _StreamState,
    output_policy: OutputPolicy,
) -> AsyncIterator[dict[str, Any]]:
    """Normalize target events while keeping tool traces independently visible."""

    stream = target.astream(
        graph_input,
        config=config,
        stream_mode=["messages", "tasks", "values"],
        **_durability_options(target.astream),
    )
    async with aclosing(stream):
        async for item in stream:
            mode, value = _stream_item(item)
            if mode == "values":
                state.final_state = value
                continue
            if mode == "tasks":
                event = _langgraph_task_activity(value, state)
                if event is not None:
                    yield event
                continue
            for event in _langgraph_stream_message_events(
                value, state, output_policy
            ):
                yield event


def _langgraph_stream_message_events(
    value: Any, state: _StreamState, output_policy: OutputPolicy
) -> list[dict[str, Any]]:
    """Normalize one message-mode item and update streaming reconciliation state."""

    message, metadata = (
        value if isinstance(value, tuple) and len(value) == 2 else (value, {})
    )
    agent = _langgraph_agent(message, metadata)
    events = _start_langgraph_agent(agent, state)
    events.extend(
        _langgraph_message_items(
            message, agent, include_message=False, include_tools=False
        )
    )
    tool_events = _message_tool_events(message)
    for event in tool_events:
        identity = _event_identity(event)
        if identity not in state.tools:
            state.tools.add(identity)
            events.append(_with_langgraph_agent(event, agent))
    content = _message_text(message)
    has_tool_calls = any(
        event.get("type") == "tool_call" for event in tool_events
    )
    if content and output_policy.includes_intermediate_message(
        has_tool_calls=has_tool_calls
    ):
        # Included narration is public but is not part of the canonical reply
        # used to reconcile the graph's final values event.
        if not has_tool_calls:
            state.text += content
        events.append(
            _with_langgraph_agent(
                {"type": "message", "role": "assistant", "text": content}, agent
            )
        )
    _append_stream_metadata(
        events,
        state,
        message,
        metadata,
        output_policy,
        agent=agent,
    )
    return events


def _append_stream_metadata(
    events: list[dict[str, Any]],
    state: _StreamState,
    message: Any,
    stream_metadata: Any,
    output_policy: OutputPolicy,
    *,
    agent: str | None,
) -> None:
    """Append a new metadata projection once across repeated message chunks."""

    event = _langgraph_metadata_event(
        message, stream_metadata, output_policy, agent=agent
    )
    if event is None:
        return
    identity = _metadata_event_identity(message, event)
    if identity in state.metadata:
        return
    state.metadata.add(identity)
    events.append(event)


def _metadata_event_identity(
    message: Any, event: Mapping[str, Any]
) -> tuple[Any, ...]:
    """Deduplicate chunks without collapsing equal usage from separate model calls."""

    native_id = getattr(message, "id", None)
    call_identity = (
        native_id
        if isinstance(native_id, str) and native_id
        else id(message)
    )
    return (call_identity, *_event_identity(event))


def _start_langgraph_agent(
    agent: str | None, state: _StreamState
) -> list[dict[str, Any]]:
    """Open one graph-node lifecycle span at its first observable activity."""

    if agent is None or agent in state.active_agents:
        return []
    state.active_agents.add(agent)
    return [_langgraph_activity(agent, "started")]


def _langgraph_agent(message: Any, metadata: Any) -> str | None:
    """Read the executing node identity supplied by LangGraph's message stream."""

    if isinstance(metadata, Mapping):
        for key in ("langgraph_node", "node"):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                return value
    if not _is_ai_message(message):
        # ToolMessage.name identifies the invoked tool, not the agent or graph
        # node that owns it. Only AI messages can carry a fallback agent name.
        return None
    name = getattr(message, "name", None)
    return name if isinstance(name, str) and name else None


def _with_langgraph_agent(
    event: dict[str, Any], agent: str | None
) -> dict[str, Any]:
    """Attach a graph node identity without retaining native stream metadata."""

    return {**event, **({"agent": agent} if agent else {})}


def _langgraph_activity(agent: str, activity: str) -> dict[str, Any]:
    """Create one framework-neutral graph-node lifecycle event."""

    return {"type": "agent_activity", "agent": agent, "activity": activity}


def _langgraph_task_activity(
    task: Any, state: _StreamState
) -> dict[str, Any] | None:
    """Project an authoritative task lifecycle without its data or identifiers."""

    if not isinstance(task, Mapping):
        return None
    agent = task.get("name")
    if not isinstance(agent, str) or not agent or agent.startswith("__"):
        return None
    if "input" in task:
        message_fallback = (
            agent in state.active_agents
            and agent not in state.active_task_counts
        )
        state.active_task_counts[agent] = (
            state.active_task_counts.get(agent, 0) + 1
        )
        state.active_agents.add(agent)
        if message_fallback:
            return None
        return _langgraph_activity(agent, "started")
    activity = _langgraph_task_terminal_activity(task)
    if activity is None:
        return None
    _finish_langgraph_task(agent, state)
    return _langgraph_activity(agent, activity)


def _langgraph_task_terminal_activity(task: Mapping[str, Any]) -> str | None:
    """Classify only documented task terminal fields without exposing their values."""

    if not {"error", "interrupts", "result"}.issubset(task):
        return None
    if task.get("error"):
        return "failed"
    if task.get("interrupts"):
        return "interrupted"
    return "completed"


def _finish_langgraph_task(agent: str, state: _StreamState) -> None:
    """Close one same-named task while preserving concurrent task activity."""

    count = state.active_task_counts.get(agent)
    if count is not None and count > 1:
        state.active_task_counts[agent] = count - 1
        return
    state.active_task_counts.pop(agent, None)
    state.active_agents.discard(agent)


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
            events.append(
                _with_langgraph_agent(
                    {"type": "message", "role": "assistant", "text": delta},
                    _reconciled_stream_agent(
                        state, turn_start=turn_start
                    ),
                )
            )
    for event in _tool_events(state.final_state, turn_start=turn_start):
        identity = _event_identity(event)
        if identity not in state.tools:
            state.tools.add(identity)
            events.append(event)
    events.extend(
        _unstreamed_metadata_events(application, state, turn_start=turn_start)
    )
    # A missing node update is treated as completion only after the caller has
    # ruled out a durable interrupt and reconciled any final answer text.
    events.extend(_complete_stream_agents(state))
    if public_result is not None:
        events.append(
            {
                "type": "graph_output",
                "output": public_result,
                "result": public_result,
            }
        )
    return events


def _unstreamed_metadata_events(
    application: CompiledApplication,
    state: _StreamState,
    *,
    turn_start: int,
) -> list[dict[str, Any]]:
    """Fall back to final messages only when no metadata streamed directly."""

    if state.metadata:
        return []
    return _turn_metadata_events(
        state.final_state,
        turn_start=turn_start,
        output_policy=application.output_policy,
    )


def _complete_stream_agents(state: _StreamState) -> list[dict[str, Any]]:
    """Close remaining successful tasks after final output reconciliation."""

    events: list[dict[str, Any]] = []
    for agent in sorted(state.active_agents):
        count = max(state.active_task_counts.get(agent, 1), 1)
        events.extend(
            _langgraph_activity(agent, "completed") for _ in range(count)
        )
    state.active_agents.clear()
    state.active_task_counts.clear()
    return events


def _reconciled_stream_agent(
    state: _StreamState, *, turn_start: int
) -> str | None:
    """Attribute final reconciliation only when its agent identity is unambiguous."""

    agent = _last_turn_agent(state.final_state, turn_start=turn_start)
    if agent is not None:
        return agent
    if len(state.active_agents) == 1:
        return next(iter(state.active_agents))
    return None


def _mcp_client_type() -> Any:
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError as exc:  # pragma: no cover - optional backend
        raise RuntimeError(
            "LangGraph MCP connections require langchain-mcp-adapters"
        ) from exc
    return MultiServerMCPClient


def _langgraph_mcp_marker(
    client_name: str, tool_name: str, tool: Any
) -> Any:
    """Route native BaseTool and context calls through one governed marker."""

    original = getattr(tool, "ainvoke", None)
    if not callable(original):
        raise TypeError(
            f"LangGraph MCP tool {tool_name!r} must expose async invocation"
        )
    owner = object()

    async def operation(arguments: Mapping[str, Any]) -> Any:
        native = _MCP_NATIVE_CALL_SCOPE.get()
        if native is not None and native[0] is owner:
            _, tool_input, config, kwargs = native
            result = await original(tool_input, config=config, **dict(kwargs))
            return _checked_mcp_tool_result(result)
        result = await original(
            _context_mcp_tool_call(tool_name, arguments)
        )
        return _context_mcp_tool_result(result)

    from .agent_principal import required_permissions

    marker = _managed_mcp_tool(
        client_name,
        tool_name,
        operation,
        required_permissions=tuple(required_permissions(tool)),
    )

    async def routed(
        tool_input: Any,
        config: Any = None,
        **kwargs: Any,
    ) -> Any:
        if not _has_agent_context():
            # Direct/advanced framework ownership has no managed context to
            # revoke. Preserve its native execution and adapter approval path.
            return await original(tool_input, config=config, **kwargs)
        arguments = _mcp_tool_arguments(tool_input)
        token = _MCP_NATIVE_CALL_SCOPE.set(
            (owner, tool_input, config, dict(kwargs))
        )
        try:
            try:
                result = await marker.invoke(arguments)
            except MCPToolCallError:
                if not _is_native_tool_call(tool_input):
                    raise
                return _native_mcp_tool_output(
                    tool,
                    tool_input,
                    "managed MCP tool returned an error",
                    status="error",
                )
            return _native_mcp_tool_output(tool, tool_input, result)
        finally:
            _MCP_NATIVE_CALL_SCOPE.reset(token)

    routed.__name__ = tool_name
    _mark_governed_mcp_operation(routed)
    # The adapter creates each discovered tool for this driver, so replacing
    # its entry point cannot affect another application or connection owner.
    object.__setattr__(tool, "ainvoke", routed)
    return marker


def _context_mcp_tool_call(
    tool_name: str, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    """Preserve LangChain's error status for context-originated MCP calls."""

    # A plain BaseTool input discards ToolMessage status and turns MCP isError
    # into ordinary content. The public ToolCall envelope retains that status.
    return {
        "type": "tool_call",
        "name": tool_name,
        "args": dict(arguments),
        "id": f"harnest-mcp-{uuid.uuid4().hex}",
    }


def _context_mcp_tool_result(result: Any) -> Any:
    """Return successful content and turn provider errors into safe lifecycle flow."""

    _checked_mcp_tool_result(result)
    if getattr(result, "type", None) == "tool" and hasattr(result, "content"):
        return result.content
    return result


def _checked_mcp_tool_result(result: Any) -> Any:
    """Detach provider error results before authored lifecycle observers see them."""

    if _mcp_result_failed(result):
        # ToolMessage content may contain an arbitrary provider response. The
        # replacement error deliberately retains no reference to that object.
        raise MCPToolCallError("managed MCP tool returned an error")
    return result


def _native_mcp_tool_output(
    tool: Any, tool_input: Any, result: Any, *, status: str = "success"
) -> Any:
    """Preserve native ToolCall correlation for finish, recovery, and failure."""

    from .backends.langgraph import _native_tool_call_output

    return _native_tool_call_output(
        tool, tool_input, result, status=status
    )


def _remote_mcp_tool_name(native_name: str, capability_id: str) -> str:
    """Recover the adapter's discovered name for the public client facade."""

    prefix = f"{capability_id}_"
    if not native_name.startswith(prefix):
        raise ValueError(
            f"LangGraph MCP tool {native_name!r} is missing its client prefix"
        )
    return native_name.removeprefix(prefix)


def _mcp_tool_arguments(tool_input: Any) -> Mapping[str, Any]:
    """Extract user arguments while retaining the native envelope for dispatch."""

    if not isinstance(tool_input, Mapping):
        raise TypeError("LangGraph MCP tool input must be a mapping")
    if tool_input.get("type") != "tool_call":
        return tool_input
    arguments = tool_input.get("args")
    if not isinstance(arguments, Mapping):
        raise TypeError("LangGraph MCP tool-call args must be a mapping")
    return arguments


def _is_native_tool_call(tool_input: Any) -> bool:
    """Identify calls for which LangGraph requires a ToolMessage response."""

    return (
        isinstance(tool_input, Mapping)
        and tool_input.get("type") == "tool_call"
    )


def _has_agent_context() -> bool:
    """Detect the outer runtime authority without broadening direct-driver use."""

    from .context import context

    try:
        context.current()
    except RuntimeError:
        return False
    return True


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
        connection = _configured_mcp_connection(configured)
        if connection is not None:
            names.append((name, configured))
            connections[name] = connection
    return connections, names


def _configured_mcp_connection(configured: Any) -> dict[str, Any] | None:
    """Contain portable configuration failures without weakening native MCP policy."""
    try:
        return configured.to_langgraph_connection()
    except Exception as error:
        if getattr(configured, "portable", None) is None:
            raise
        configured.portable.failed(error)
        return None


async def _discover_configured_mcp_tools(
    client: Any, name: str, configured: Any, *, resources: list[Any] | None = None,
) -> list[Any]:
    """Keep portable stdio literal and contain failures within their component."""
    try:
        if getattr(configured, "portable", None) is not None and configured.transport == "stdio":
            from .agent_plugin_langgraph import PortableStdioOwner
            if resources is None:
                raise RuntimeError("portable stdio requires runtime-owned cleanup")
            owner = PortableStdioOwner(client, name, configured)
            resources.append(owner)
            return await owner.start()
        return await client.get_tools(server_name=name)
    except Exception as error:
        if getattr(configured, "portable", None) is None:
            raise
        configured.portable.failed(error)
        return []


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


def _apply_mcp_permissions(
    tools: Sequence[Any], server_name: str, configured: Any
) -> None:
    """Attach declared MCP requirements to discovered native tool objects."""

    from .agent_principal import attach_required_permissions

    prefix = f"{server_name}_"
    named = [
        (
            tool,
            str(getattr(tool, "name", "")).removeprefix(prefix),
        )
        for tool in tools
    ]
    _validate_mcp_permission_tools(
        configured.tool_permissions,
        tuple(name for _, name in named),
        server_name,
    )
    for tool, name in named:
        attach_required_permissions(
            tool, tuple(configured.required_permissions_for(name))
        )


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
        return await _invoke_governed_mcp_call(
            lambda: handler(request),
            client_name=client_name,
            tool_name=tool_name,
            arguments=request.args,
            policy=policy,
        )

    return require_approval


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


async def _graph_input(
    application: CompiledApplication,
    text_value: Any,
    state: Mapping[str, Any],
    *,
    stores: Mapping[str, AssetStore],
    scope: AssetScope,
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
    authored = _managed_authored_input(application, text_value)
    access = current_transient_media()
    if isinstance(authored, BaseModel):
        authored = await stage_stored_media(
            authored, stores=stores, scope=scope
        )
        if access is not None:
            authored = access.stage(authored)
    messages = [
        *previous_messages,
        HumanMessage(
            content=_portable_input_content(authored)
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
    if _portable_content_sequence(value):
        return [_portable_content_block(item) for item in value]
    if isinstance(value, BaseModel):
        blocks = _model_content_blocks(value)
        return blocks or _model_input_text(value)
    if isinstance(value, Mapping):
        blocks = _mapping_content_blocks(value)
        return blocks or _model_input_text(value)
    return _model_input_text(value)


def _portable_content_sequence(value: Any) -> bool:
    """Recognize a non-empty authored content sequence."""

    return isinstance(value, (list, tuple)) and bool(value) and all(
        isinstance(item, _CONTENT_PART_TYPES) for item in value
    )


def _mapping_content_blocks(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Project one structured mapping into ordered model input blocks."""

    ordinary, blocks = _extract_content(value)
    if not blocks:
        return []
    if ordinary is not _NO_ORDINARY_CONTENT:
        blocks.insert(
            0, {"type": "text", "text": _model_input_text(ordinary)}
        )
    return blocks


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
    if (
        transient_media_lease_id(value) is not None
        or is_transient_media_placeholder(value)
    ):
        return _NO_ORDINARY_CONTENT, [dict(value)]
    if stored_media_reference(value) is not None:
        return _NO_ORDINARY_CONTENT, [dict(value)]
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


@asynccontextmanager
async def _invocation_session(
    store: SessionStore,
    request: InvocationRequest,
    session_id: str,
) -> AsyncIterator[SessionLease]:
    """Acquire or reuse the one invocation lease shared with outer hooks."""

    async with invocation_session_context(
        store,
        framework="langgraph",
        user_id=request.user_id,
        session_id=session_id,
        invocation_id=request.invocation_id,
    ) as lease:
        # This adapter always supplies a portable store, so the helper cannot
        # yield its no-store sentinel at this managed boundary.
        if lease is None:  # pragma: no cover - protected by the store argument
            raise RuntimeError("LangGraph session lease is unavailable")
        yield lease


def _visible_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(json_value(value), ensure_ascii=False)


def _message_text(message: Any) -> str:
    if not _is_ai_message(message):
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


def _is_ai_message(message: Any) -> bool:
    """Accept AI envelopes and untyped test/provider-compatible message values."""

    return getattr(message, "type", None) in {
        None,
        "ai",
        "AIMessage",
        "AIMessageChunk",
    }


def _message_thinking(message: Any) -> str:
    """Extract provider-emitted reasoning text without exposing signatures or metadata."""

    if not _is_ai_message(message):
        return ""
    content = getattr(message, "content", message)
    parts: list[str] = []
    if isinstance(content, (list, tuple)):
        for block in content:
            text = _reasoning_block_text(block)
            if text:
                parts.append(text)
    if parts:
        return "".join(parts)
    additional = getattr(message, "additional_kwargs", None)
    reasoning = (
        additional.get("reasoning_content")
        if isinstance(additional, Mapping)
        else None
    )
    return reasoning if isinstance(reasoning, str) else ""


def _reasoning_block_text(block: Any) -> str:
    """Project text from a reasoning block and discard provider-only fields."""

    if not isinstance(block, Mapping) or block.get("type") not in {
        "thinking",
        "reasoning",
    }:
        return ""
    for key in ("thinking", "reasoning", "text"):
        value = block.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _langgraph_metadata_event(
    message: Any,
    stream_metadata: Any,
    output_policy: OutputPolicy,
    *,
    agent: str | None,
) -> dict[str, Any] | None:
    """Normalize one AI message's reported metadata under the shared model."""

    if not _is_ai_message(message):
        return None
    response = _message_mapping(message, "response_metadata")
    usage_metadata = _message_mapping(message, "usage_metadata")
    additional = _message_mapping(message, "additional_kwargs")
    stream = stream_metadata if isinstance(stream_metadata, Mapping) else {}
    usage = _langgraph_token_usage(usage_metadata, response)
    model = _first_text(
        stream,
        ("ls_model_name", "model_name", "model"),
        response,
        ("model_name", "model", "model_id"),
    )
    provider = _first_text(
        stream,
        ("ls_provider", "model_provider", "provider"),
        response,
        ("model_provider", "provider", "provider_name"),
    )
    finish_reason = _first_text(
        response,
        ("finish_reason", "stop_reason"),
        additional,
        ("finish_reason", "stop_reason"),
    )
    raw = _langgraph_raw_metadata(
        stream, response, usage_metadata, additional, output_policy
    )
    if all(value is None for value in (usage, model, provider, finish_reason, raw)):
        return None
    return AgentMetadata(
        framework="langgraph",
        usage=usage,
        model=model,
        provider=provider,
        finish_reason=finish_reason,
        raw=raw,
    )._as_runtime_event(agent=agent)


def _message_mapping(message: Any, name: str) -> Mapping[str, Any]:
    """Read a LangChain mapping field without accepting arbitrary objects."""

    value = getattr(message, name, None)
    return value if isinstance(value, Mapping) else {}


def _langgraph_token_usage(
    usage_metadata: Mapping[str, Any], response: Mapping[str, Any]
) -> TokenUsage | None:
    """Prefer standardized usage, then recognized response token-usage shapes."""

    if usage := _token_usage_mapping(usage_metadata):
        return usage
    for key in ("token_usage", "usage"):
        candidate = response.get(key)
        if isinstance(candidate, Mapping) and (
            usage := _token_usage_mapping(candidate)
        ):
            return usage
    return _token_usage_mapping(response)


def _token_usage_mapping(value: Mapping[str, Any]) -> TokenUsage | None:
    """Normalize exact integer counts across common LangChain provider aliases."""

    return _reported_token_usage(
        _first_token_count(
            value,
            (
                "input_tokens",
                "prompt_tokens",
                "input_token_count",
                "prompt_token_count",
            ),
        ),
        _first_token_count(
            value,
            (
                "output_tokens",
                "completion_tokens",
                "output_token_count",
                "completion_token_count",
                "candidates_token_count",
            ),
        ),
        _first_token_count(value, ("total_tokens", "total_token_count")),
    )


def _first_token_count(
    source: Mapping[str, Any], names: Sequence[str]
) -> int | None:
    """Return the first exact non-negative integer among compatible aliases."""

    for name in names:
        value = source.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _first_text(
    primary: Mapping[str, Any],
    primary_names: Sequence[str],
    secondary: Mapping[str, Any],
    secondary_names: Sequence[str],
) -> str | None:
    """Select the first non-empty provider-reported text label."""

    for source, names in (
        (primary, primary_names),
        (secondary, secondary_names),
    ):
        for name in names:
            value = source.get(name)
            if isinstance(value, str) and value:
                return value
    return None


def _langgraph_raw_metadata(
    stream: Mapping[str, Any],
    response: Mapping[str, Any],
    usage: Mapping[str, Any],
    additional: Mapping[str, Any],
    output_policy: OutputPolicy,
) -> Mapping[str, Any] | None:
    """Namespace native mappings only under the explicit raw-output policy."""

    if output_policy.agent_metadata != "raw":
        return None
    raw = {
        name: json_value(dict(value), unsupported="string")
        for name, value in (
            ("stream_metadata", stream),
            ("response_metadata", response),
            ("usage_metadata", usage),
            ("additional_kwargs", additional),
        )
        if value
    }
    return raw or None


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
    sanitized, lease_ids, was_json = _message_transient_value(content)
    if lease_ids:
        content = (
            json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))
            if was_json
            else sanitized
        )
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
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError):
            return content
        safe = sanitize_stored_media(sanitize_transient_media(parsed)[0])
        return json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
    if content is None:
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
        result = json_value(getattr(message, "content", None))
        sanitized, lease_ids, was_json = _message_transient_value(result)
        sanitized = sanitize_stored_media(sanitized)
        if was_json:
            result = (
                json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))
            )
        elif lease_ids or sanitized != result:
            result = sanitized
        events.append(
            {
                "type": "tool_result",
                "id": getattr(message, "tool_call_id", None),
                "name": getattr(message, "name", None),
                "result": result,
            }
        )
    return events


def _tool_events(
    result: Any, *, turn_start: int = 0
) -> list[dict[str, Any]]:
    """Return tool activity from the current turn without replaying session history."""

    if not isinstance(result, Mapping):
        return []
    messages = result.get("messages")
    if not isinstance(messages, (list, tuple)):
        return []
    events = []
    for message in _current_turn_messages(messages, turn_start=turn_start):
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
        events = _included_message_events(
            result, turn_start=turn_start, output_policy=output_policy
        )
    else:
        events = _turn_activity_events(
            result, turn_start=turn_start, output_policy=output_policy
        )
    if text_value and not _events_end_with_text(events, text_value):
        event = _with_langgraph_agent(
            {"type": "message", "role": "assistant", "text": text_value},
            _last_turn_agent(result, turn_start=turn_start),
        )
        _insert_final_message(events, event)
    if public_result is not None:
        events.append(
            {
                "type": "graph_output",
                "output": public_result,
                "result": public_result,
            }
        )
    return events


def _included_message_events(
    result: Any, *, turn_start: int, output_policy: OutputPolicy
) -> list[dict[str, Any]]:
    """Expose current-turn activity and narration without replaying session history."""

    return _turn_message_events(
        result,
        turn_start=turn_start,
        include_messages=True,
        output_policy=output_policy,
    )


def _turn_activity_events(
    result: Any, *, turn_start: int, output_policy: OutputPolicy
) -> list[dict[str, Any]]:
    """Collect reasoning and tools from only the current non-streaming graph turn."""

    return _turn_message_events(
        result,
        turn_start=turn_start,
        include_messages=False,
        output_policy=output_policy,
    )


def _turn_message_events(
    result: Any,
    *,
    turn_start: int,
    include_messages: bool,
    output_policy: OutputPolicy,
) -> list[dict[str, Any]]:
    """Project public current-turn events and bounded named-agent lifecycles."""

    if not isinstance(result, Mapping):
        return []
    messages = result.get("messages")
    if not isinstance(messages, (list, tuple)):
        return []
    events: list[dict[str, Any]] = []
    active_agents: list[str] = []
    for message in _current_turn_messages(messages, turn_start=turn_start):
        agent = _langgraph_agent(message, {})
        if agent is not None and agent not in active_agents:
            active_agents.append(agent)
            events.append(_langgraph_activity(agent, "started"))
        events.extend(
            _langgraph_message_items(
                message,
                agent,
                include_message=include_messages,
                output_policy=output_policy,
            )
        )
    events.extend(
        _langgraph_activity(agent, "completed") for agent in active_agents
    )
    return events


def _turn_metadata_events(
    result: Any, *, turn_start: int, output_policy: OutputPolicy
) -> list[dict[str, Any]]:
    """Project final-state metadata when a target omits message stream events."""

    if not isinstance(result, Mapping):
        return []
    messages = result.get("messages")
    if not isinstance(messages, (list, tuple)):
        return []
    events: list[dict[str, Any]] = []
    for message in _current_turn_messages(messages, turn_start=turn_start):
        event = _langgraph_metadata_event(
            message,
            {},
            output_policy,
            agent=_langgraph_agent(message, {}),
        )
        if event is not None:
            events.append(event)
    return events


def _langgraph_message_items(
    message: Any,
    agent: str | None,
    *,
    include_message: bool,
    include_tools: bool = True,
    output_policy: OutputPolicy | None = None,
) -> list[dict[str, Any]]:
    """Project public reasoning, optional narration, and tool activity from a message."""

    events: list[dict[str, Any]] = []
    thinking = _message_thinking(message)
    if thinking:
        events.append(
            _with_langgraph_agent({"type": "thinking", "text": thinking}, agent)
        )
    content = _message_text(message) if include_message else ""
    if content:
        events.append(
            _with_langgraph_agent(
                {"type": "message", "role": "assistant", "text": content}, agent
            )
        )
    if include_tools:
        events.extend(
            _with_langgraph_agent(event, agent)
            for event in _message_tool_events(message)
        )
    if output_policy is not None:
        metadata = _langgraph_metadata_event(
            message, {}, output_policy, agent=agent
        )
        if metadata is not None:
            events.append(metadata)
    return events


def _current_turn_messages(
    messages: Sequence[Any], *, turn_start: int
) -> Sequence[Any]:
    """Slice native history while tolerating advanced targets that return deltas."""

    if turn_start <= 0 or turn_start > len(messages):
        return messages
    preceding_type = getattr(messages[turn_start - 1], "type", None)
    # Managed LangGraph results retain the authored human message at the turn
    # boundary. Advanced targets may instead return only their output delta.
    return messages[turn_start:] if preceding_type in {"human", "user"} else messages


def _last_turn_agent(result: Any, *, turn_start: int) -> str | None:
    """Find the agent responsible for a non-streaming turn's canonical reply."""

    if not isinstance(result, Mapping):
        return None
    messages = result.get("messages")
    if not isinstance(messages, (list, tuple)):
        return None
    current = _current_turn_messages(messages, turn_start=turn_start)
    return _langgraph_agent(current[-1], {}) if current else None


def _insert_final_message(
    events: list[dict[str, Any]], event: dict[str, Any]
) -> None:
    """Place a canonical reply before its metadata and lifecycle completion."""

    index = len(events)
    while (
        index
        and (
            events[index - 1].get("type") == "agent_metadata"
            or (
                events[index - 1].get("type") == "agent_activity"
                and events[index - 1].get("activity") == "completed"
            )
        )
    ):
        index -= 1
    events.insert(index, event)


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
    portable = {key: value for key, value in event.items() if key != "agent"}
    return (
        event.get("type"),
        event.get("id"),
        event.get("name"),
        json.dumps(json_value(portable), sort_keys=True, ensure_ascii=False),
    )


def _stream_item(item: Any) -> tuple[str, Any]:
    if isinstance(item, tuple) and len(item) == 2 and item[0] in {
        "messages",
        "tasks",
        "values",
    }:
        return item[0], item[1]
    # A target may ignore the requested multi-mode stream and yield values only.
    return "values", item


__all__ = ["LangGraphRuntimeDriver"]
