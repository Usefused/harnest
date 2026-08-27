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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .application import CompiledApplication
from .neutral_runtime import (
    AgentInfo,
    InvocationRequest,
    InvocationResult,
    RuntimeDriver,
    SessionConflictError,
    SessionRecord,
)


@dataclass(slots=True)
class _StoredSession:
    user_id: str
    state: dict[str, Any]
    created_at: str = field(default_factory=lambda: _timestamp())
    updated_at: str = field(default_factory=lambda: _timestamp())
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class LangGraphRuntimeDriver(RuntimeDriver):
    """Adapt one compiled LangGraph application to the neutral runtime.

    The driver owns one explicit in-memory session map.  Every graph execution
    receives a fresh internal ``thread_id`` so an optional LangGraph checkpointer
    cannot silently become a second source of session truth.
    """

    def __init__(
        self,
        application: CompiledApplication,
        *,
        card: Mapping[str, Any] | None = None,
        recursion_limit: int = 64,
    ) -> None:
        if not isinstance(application, CompiledApplication):
            raise TypeError("application must be a CompiledApplication")
        if application.framework != "langgraph":
            raise ValueError("LangGraphRuntimeDriver requires a LangGraph application")
        if recursion_limit < 1:
            raise ValueError("recursion_limit must be at least one")
        from .backends.langgraph import ManagedAgentPlan, ManagedGraphPlan

        plan = (
            application.target
            if isinstance(application.target, (ManagedAgentPlan, ManagedGraphPlan))
            else None
        )
        if plan is None and not callable(getattr(application.target, "ainvoke", None)):
            raise TypeError("compiled LangGraph target must expose ainvoke")

        card_value = dict(card or {})
        name = str(card_value.get("name") or application.name)
        self._application = application
        self._plan = plan
        self._target = None if plan is not None else application.target
        self._mcp_clients: list[Any] = []
        self._materialize_lock = asyncio.Lock()
        self._recursion_limit = recursion_limit
        self._info = AgentInfo(
            id=application.name,
            name=name,
            description=str(card_value.get("description") or ""),
            card=card_value,
            framework="langgraph",
            mode=application.mode,
        )
        self._sessions: dict[tuple[str, str], _StoredSession] = {}
        self._sessions_lock = asyncio.Lock()
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
        _require_identifier(session_id, "session_id")
        _require_identifier(user_id, "user_id")
        if not isinstance(state, Mapping):
            raise TypeError("state must be a mapping")
        key = (user_id, session_id)
        async with self._sessions_lock:
            if key in self._sessions:
                raise SessionConflictError("session already exists")
            stored = _StoredSession(user_id=user_id, state=dict(state))
            self._sessions[key] = stored
        return _session_record(session_id, stored)

    async def get_session(
        self, *, session_id: str, user_id: str
    ) -> SessionRecord | None:
        self._ensure_open()
        stored = self._sessions.get((user_id, session_id))
        if stored is None:
            return None
        async with stored.lock:
            return _session_record(session_id, stored)

    async def list_sessions(self, *, user_id: str) -> Sequence[SessionRecord]:
        self._ensure_open()
        _require_identifier(user_id, "user_id")
        pairs = sorted(
            (
                (session_id, stored)
                for (owner, session_id), stored in self._sessions.items()
                if owner == user_id
            ),
            key=lambda item: item[0],
        )
        records = []
        for session_id, stored in pairs:
            async with stored.lock:
                records.append(_session_record(session_id, stored))
        return tuple(records)

    async def update_session(
        self,
        *,
        session_id: str,
        user_id: str,
        state_delta: Mapping[str, Any],
    ) -> SessionRecord | None:
        self._ensure_open()
        if not isinstance(state_delta, Mapping):
            raise TypeError("state_delta must be a mapping")
        stored = self._sessions.get((user_id, session_id))
        if stored is None:
            return None
        async with stored.lock:
            stored.state.update(dict(state_delta))
            stored.updated_at = _timestamp()
            return _session_record(session_id, stored)

    async def delete_session(self, *, session_id: str, user_id: str) -> bool:
        self._ensure_open()
        async with self._sessions_lock:
            return self._sessions.pop((user_id, session_id), None) is not None

    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        """Run one graph invocation and return canonical neutral events."""

        stored, session_id = await self._session_for_request(request)
        async with stored.lock:
            self._apply_request_delta(stored, request)
            graph_input = _graph_input(
                self._application, request.input, stored.state
            )
            config = self._execution_config(session_id)
            target = await self._target_for_run()
            result = await target.ainvoke(graph_input, config=config)
            self._store_result(stored, result)

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

        stored, session_id = await self._session_for_request(request)
        async with stored.lock:
            self._apply_request_delta(stored, request)
            graph_input = _graph_input(
                self._application, request.input, stored.state
            )
            config = self._execution_config(session_id)
            final_state: Any = None
            streamed_text = ""
            emitted_tools: set[tuple[Any, ...]] = set()
            stream = target.astream(
                graph_input,
                config=config,
                stream_mode=["messages", "values"],
            )
            async with aclosing(stream):
                iterator = stream.__aiter__()
                while True:
                    try:
                        item = await iterator.__anext__()
                    except StopAsyncIteration:
                        break
                    mode, value = _stream_item(item)
                    if mode == "values":
                        final_state = value
                        continue
                    message = value[0] if isinstance(value, tuple) else value
                    for event in _message_tool_events(message):
                        identity = _event_identity(event)
                        if identity not in emitted_tools:
                            emitted_tools.add(identity)
                            yield event
                    content = _message_text(message)
                    if content:
                        streamed_text += content
                        yield {"type": "message", "role": "assistant", "text": content}

            if final_state is None:
                final_state = {}
            self._store_result(stored, final_state)

        final_text, public_result = _graph_output(
            self._application, final_state
        )
        if final_text and final_text != streamed_text:
            delta = (
                final_text[len(streamed_text) :]
                if final_text.startswith(streamed_text)
                else final_text
            )
            if delta:
                yield {"type": "message", "role": "assistant", "text": delta}
        for event in _tool_events(final_state):
            identity = _event_identity(event)
            if identity not in emitted_tools:
                emitted_tools.add(identity)
                yield event
        if public_result is not None:
            yield {
                "type": "graph_output",
                "output": public_result,
                "result": public_result,
            }

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
            async with self._sessions_lock:
                self._sessions.clear()
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
            try:
                from langchain_mcp_adapters.client import MultiServerMCPClient
            except ImportError as exc:  # pragma: no cover - optional backend
                raise RuntimeError(
                    "LangGraph MCP connections require langchain-mcp-adapters"
                ) from exc

            plan = self._plan
            if plan is None:  # pragma: no cover - protected by constructor
                raise RuntimeError("LangGraph target is unavailable")
            try:
                from .backends.langgraph import (
                    ManagedAgentPlan,
                    managed_graph_mcp_clients,
                    materialize_agent,
                    materialize_graph,
                )

                configured_groups = (
                    (tuple(plan.definition.mcp),)
                    if isinstance(plan, ManagedAgentPlan)
                    else managed_graph_mcp_clients(plan)
                )
                tool_groups = []
                resolved_groups: list[tuple[tuple[Any, ...], list[Any]]] = []
                for configured_group in configured_groups:
                    shared_tools = next(
                        (
                            tools
                            for existing, tools in resolved_groups
                            if existing == configured_group
                        ),
                        None,
                    )
                    if shared_tools is not None:
                        tool_groups.append(shared_tools)
                        continue
                    connections: dict[str, dict[str, Any]] = {}
                    names = []
                    for index, configured in enumerate(configured_group):
                        name = configured.tool_name_prefix or f"mcp_{index + 1}"
                        if name in connections:
                            raise ValueError(
                                f"duplicate LangGraph MCP client name {name!r}"
                            )
                        names.append((name, configured))
                        connections[name] = configured.to_langgraph_connection()

                    client = MultiServerMCPClient(
                        connections, tool_name_prefix=True
                    )
                    self._mcp_clients.append(client)
                    tools = []
                    for server_name, configured in names:
                        discovered = await client.get_tools(
                            server_name=server_name
                        )
                        tools.extend(
                            _filtered_mcp_tools(
                                discovered,
                                server_name=server_name,
                                allowed=configured.tool_filter,
                            )
                        )
                    resolved_groups.append((configured_group, tools))
                    tool_groups.append(tools)

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
            return self._target

    async def _session_for_request(
        self, request: InvocationRequest
    ) -> tuple[_StoredSession, str]:
        self._ensure_open()
        user_id = request.user_id
        _require_identifier(user_id, "request.user_id")
        requested_id = request.session_id
        if requested_id is not None:
            _require_identifier(requested_id, "request.session_id")
            stored = self._sessions.get((user_id, requested_id))
            if stored is None:
                raise KeyError("session not found")
            return stored, requested_id

        session_id = uuid.uuid4().hex
        stored = _StoredSession(user_id=user_id, state={})
        async with self._sessions_lock:
            self._sessions[(user_id, session_id)] = stored
        return stored, session_id

    def _apply_request_delta(
        self, stored: _StoredSession, request: InvocationRequest
    ) -> None:
        delta = request.state_delta
        if delta:
            stored.state.update(dict(delta))
            stored.updated_at = _timestamp()

    def _store_result(self, stored: _StoredSession, result: Any) -> None:
        if isinstance(result, Mapping):
            # ``ainvoke`` returns the complete graph state.  Replacing instead of
            # merging also removes transient keys intentionally cleared by a graph.
            stored.state = dict(result)
        else:
            stored.state = {"value": result}
        stored.updated_at = _timestamp()

    def _execution_config(self, session_id: str) -> dict[str, Any]:
        return {
            "configurable": {
                # Never reuse the public session id as checkpoint identity: the
                # explicit session map above is the sole state authority.
                "thread_id": f"{session_id}:{uuid.uuid4().hex}",
            },
            "recursion_limit": self._recursion_limit,
        }

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("LangGraph runtime driver is closed")


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
    closer = getattr(resource, "aclose", None)
    if not callable(closer):
        closer = getattr(resource, "close", None)
    if not callable(closer):
        return
    result = closer()
    if inspect.isawaitable(result):
        await result


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session_record(session_id: str, stored: _StoredSession) -> SessionRecord:
    return SessionRecord(
        id=session_id,
        user_id=stored.user_id,
        state=_json_value(stored.state),
        created_at=stored.created_at,
        updated_at=stored.updated_at,
    )


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
            **dict(state),
            "value": text_value,
            "messages": messages,
            "route": None,
        }
    return {**dict(state), "messages": messages}


def _graph_output(
    application: CompiledApplication, result: Any
) -> tuple[str, Any]:
    adapted = result
    if application.bridge is not None and application.bridge.output_adapter is not None:
        adapted = application.bridge.output_adapter(result)
    if adapted is not result:
        if isinstance(adapted, str):
            return adapted, adapted
        return _visible_value(adapted), _json_value(adapted)
    if isinstance(result, Mapping):
        structured = {
            str(key): _json_value(value)
            for key, value in result.items()
            if key != "messages"
        }
        public_result = structured or None
        messages = result.get("messages")
        if isinstance(messages, (list, tuple)) and messages:
            content = _message_text(messages[-1])
            if content:
                return content, public_result
        value = result.get("value")
        if isinstance(value, str):
            return value, public_result
        if value is not None:
            return _visible_value(value), public_result
    if isinstance(result, str):
        return result, result
    return _visible_value(result), _json_value(result)


def _visible_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(_json_value(value), ensure_ascii=False)


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
                    "arguments": _json_value(call.get("args", {})),
                }
            )
    if getattr(message, "type", None) == "tool":
        events.append(
            {
                "type": "tool_result",
                "id": getattr(message, "tool_call_id", None),
                "name": getattr(message, "name", None),
                "result": _json_value(getattr(message, "content", None)),
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
        json.dumps(_json_value(event), sort_keys=True, ensure_ascii=False),
    )


def _stream_item(item: Any) -> tuple[str, Any]:
    if isinstance(item, tuple) and len(item) == 2 and item[0] in {
        "messages",
        "values",
    }:
        return item[0], item[1]
    # A target may ignore the requested multi-mode stream and yield values only.
    return "values", item


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _json_value(model_dump(mode="json", by_alias=True))
    return str(value)


__all__ = ["LangGraphRuntimeDriver"]
