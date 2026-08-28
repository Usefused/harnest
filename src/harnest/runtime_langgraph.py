"""LangGraph implementation of Harnest's framework-neutral runtime driver.

This module deliberately contains no HTTP concerns.  It translates between the
portable runtime contract and LangGraph's state/message protocol while keeping
the public Harnest session state authoritative.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from typing import Any

from ._json import json_value
from .approval import (
    ApprovalPolicy,
    authorize_mcp,
    record_approved_execution,
    record_approved_failure,
)
from .application import CompiledApplication
from .checkpoint import CheckpointStore, HarnestStore
from .model_lifecycle import close_litellm_lifecycles
from .mcp import _validate_approval_tools
from .neutral_runtime import (
    AgentInfo,
    InvocationRequest,
    InvocationResult,
    RuntimeDriver,
    SessionRecord,
)
from .session import InMemorySessionStore, SessionLease, SessionStore


_TURN_START_KEY = "_harnest_turn_start"
_SESSION_STATE_KEY = "_harnest_state"
_GRAPH_RUNTIME_KEYS = frozenset(
    {"messages", "value", "route", _TURN_START_KEY, _SESSION_STATE_KEY}
)


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
    ) -> None:
        plan = _runtime_plan(application, recursion_limit)
        card_value = dict(card or {})
        self._application = application
        self._plan = plan
        self._target = None if plan is not None else application.target
        self._mcp_clients: list[Any] = []
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

    async def list_sessions(self, *, user_id: str) -> Sequence[SessionRecord]:
        self._ensure_open()
        records = await self._session_store.list(user_id=user_id)
        return tuple(self._public_session(record) for record in records)

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
            try:
                result = await target.ainvoke(graph_input, config=config)
                await self._store_result(session, result)
            except BaseException:
                await self._finish_checkpoint(request.invocation_id, "failed")
                raise
            await self._finish_checkpoint(request.invocation_id, "completed")

        text_value, public_result = _graph_output(self._application, result)
        events = _result_events(result, text_value, public_result)
        return InvocationResult(
            text=text_value,
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
            try:
                async for event in _target_stream(target, graph_input, config, state):
                    yield event
                state.final_state = (
                    state.final_state if state.final_state is not None else {}
                )
                await self._store_result(session, state.final_state)
            except BaseException:
                await self._finish_checkpoint(request.invocation_id, "failed")
                raise
            await self._finish_checkpoint(request.invocation_id, "completed")

        for event in _final_stream_events(self._application, state):
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
                for client in reversed(self._mcp_clients):
                    try:
                        if client is not target:
                            await _close_resource(client)
                    except BaseException as exc:
                        if failure is None:
                            failure = exc
                self._mcp_clients.clear()
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
            self._target = (
                materialize_agent(plan, tool_groups[0])
                if isinstance(plan, ManagedAgentPlan)
                else materialize_graph(plan, tool_groups)
            )
        except BaseException:
            for client in reversed(self._mcp_clients):
                await _close_resource(client)
            self._mcp_clients.clear()
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

    async def _store_result(self, session: SessionLease, result: Any) -> None:
        if isinstance(result, Mapping):
            # ``ainvoke`` returns the complete graph state.  Replacing instead of
            # merging also removes transient keys intentionally cleared by a graph.
            state = dict(result)
            state.pop(_TURN_START_KEY, None)
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
        if self._application.kind != "graph":
            return record
        return replace(record, state=_managed_graph_user_state(record.state))


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


def _agent_info(application: CompiledApplication, card: dict[str, Any]) -> AgentInfo:
    return AgentInfo(
        id=application.name,
        name=str(card.get("name") or application.name),
        description=str(card.get("description") or ""),
        card=card,
        framework="langgraph",
        mode=application.mode,
    )


async def _target_stream(
    target: Any, graph_input: Any, config: Mapping[str, Any], state: _StreamState
) -> AsyncIterator[dict[str, Any]]:
    stream = target.astream(graph_input, config=config, stream_mode=["messages", "values"])
    async with aclosing(stream):
        async for item in stream:
            mode, value = _stream_item(item)
            if mode == "values":
                state.final_state = value
                continue
            message = value[0] if isinstance(value, tuple) else value
            for event in _message_tool_events(message):
                identity = _event_identity(event)
                if identity not in state.tools:
                    state.tools.add(identity)
                    yield event
            content = _message_text(message)
            if content:
                state.text += content
                yield {"type": "message", "role": "assistant", "text": content}


def _final_stream_events(
    application: CompiledApplication, state: _StreamState
) -> list[dict[str, Any]]:
    final_text, public_result = _graph_output(application, state.final_state)
    events: list[dict[str, Any]] = []
    if final_text and final_text != state.text:
        delta = final_text[len(state.text):] if final_text.startswith(state.text) else final_text
        if delta:
            events.append({"type": "message", "role": "assistant", "text": delta})
    for event in _tool_events(state.final_state):
        identity = _event_identity(event)
        if identity not in state.tools:
            state.tools.add(identity)
            events.append(event)
    if public_result is not None:
        events.append({"type": "graph_output", "output": public_result, "result": public_result})
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
    text_value: str,
    state: Mapping[str, Any],
) -> Any:
    if application.bridge is not None and application.bridge.input_adapter is not None:
        return application.bridge.input_adapter(text_value, state)
    try:
        from langchain_core.messages import HumanMessage
    except ImportError as exc:  # pragma: no cover - optional backend dependency
        raise RuntimeError("LangGraph serving requires langchain-core") from exc
    previous_messages = state.get("messages", ())
    if not isinstance(previous_messages, (list, tuple)):
        previous_messages = ()
    messages = [*previous_messages, HumanMessage(content=text_value)]
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


def _graph_output(
    application: CompiledApplication, result: Any
) -> tuple[str, Any]:
    adapter = application.bridge.output_adapter if application.bridge is not None else None
    adapted = adapter(result) if adapter is not None else result
    if adapted is not result:
        return _adapted_output(adapted)
    if isinstance(result, Mapping):
        return _mapping_output(result)
    if isinstance(result, str):
        return result, result
    return _visible_value(result), json_value(result)


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
    result: Any, text_value: str, public_result: Any
) -> list[dict[str, Any]]:
    events = _tool_events(result)
    if text_value:
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
