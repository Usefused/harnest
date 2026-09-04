"""Lower provider-neutral Harnest graphs to executable LangGraph applications."""

from __future__ import annotations

import inspect
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Annotated, Any, TypedDict

from ..agent import AgentDefinition, _AdvancedAgentDefinition
from ..durable import is_durable_tool, langgraph_durable_callable
from ..graph import (
    START,
    Edge,
    Event,
    Graph,
    GraphContext,
    Join,
    _model_input_text,
    call_graph_node,
)
from ..model_lifecycle import propagate_litellm_lifecycles
from ..model_hooks import bind_model_extension
from ..mcp_context import _is_governed_mcp_operation
from ..sandbox_assignments import assigned_sandboxes
from ..structured import provider_output_schema
from ..tool_arguments import invalid_argument_error, unknown_argument_error
from ..tool_lifecycle import wrap_lifecycle_tool


_AGENT_PRINCIPAL_PROJECTION_COMPLETE = (
    "__harnest_agent_principal_projection_complete__"
)


def _langgraph_types():
    try:
        from langgraph.graph import (
            END,
            START as LANGGRAPH_START,
            StateGraph,
            add_messages,
        )
        from langgraph.pregel import Pregel
    except ImportError as exc:  # pragma: no cover - depends on optional backend
        raise RuntimeError(
            "the LangGraph backend requires langgraph and langchain-core"
        ) from exc
    return END, LANGGRAPH_START, StateGraph, add_messages, Pregel


def passthrough_native(value: Any) -> Any:
    """Validate and return an already executable local LangGraph application.

    ``CompiledStateGraph`` and Functional API ``@entrypoint`` applications both
    inherit LangGraph's public ``Pregel`` runtime. Remote graphs and generic
    LangChain runnables are deliberately not accepted as self-contained native
    applications.
    """

    *_, Pregel = _langgraph_types()
    if not isinstance(value, Pregel):
        raise TypeError(
            "native LangGraph application must be an executable Pregel instance"
        )
    return value.validate()


def _resolve_langchain_model(model: Any) -> Any:
    """Translate Harnest models while preserving already-native instances."""

    try:
        from langchain_core.language_models import BaseChatModel
    except ImportError as exc:  # pragma: no cover - optional backend dependency
        raise RuntimeError(
            "building a LangGraph agent requires langchain-core"
        ) from exc

    from ..model import ModelConnector

    if isinstance(model, ModelConnector):
        model = model.build_langgraph()
    if isinstance(model, (str, BaseChatModel)):
        return model

    raise TypeError(
        "LangGraph agents require a model string, LangChain BaseChatModel, or "
        "Harnest ModelConnector with a LangGraph adapter"
    )


def _langchain_tools(values: Sequence[Any]) -> list[Any]:
    """Adapt Harnest callables while preserving already-native LangChain tools."""

    try:
        from langchain_core.tools import BaseTool, tool as langchain_tool
    except ImportError as exc:  # pragma: no cover - optional backend dependency
        raise RuntimeError(
            "building a LangGraph agent requires langchain-core"
        ) from exc

    result = []
    for value in values:
        if isinstance(value, BaseTool):
            native = _strict_langchain_tool(_govern_langchain_base_tool(value))
            result.append(_retain_tool_permissions(value, native))
        elif isinstance(value, Mapping):
            result.append(dict(value))
        elif callable(value):
            runtime_value = (
                langgraph_durable_callable(value)
                if is_durable_tool(value)
                else value
            )
            native = _strict_langchain_tool(langchain_tool(runtime_value))
            result.append(_retain_tool_permissions(value, native))
        else:
            raise TypeError(
                "LangGraph tools must be callables, BaseTool instances, or mappings"
            )
    return result


def _retain_tool_permissions(source: Any, target: Any) -> Any:
    """Copy permissioned-tool metadata onto LangChain's native wrapper."""

    from ..agent_principal import attach_required_permissions, required_permissions

    permissions = required_permissions(source)
    return (
        attach_required_permissions(target, tuple(permissions))
        if permissions
        else target
    )


def _strict_langchain_tool(tool: Any) -> Any:
    """Reject extra model arguments before LangChain can discard them."""

    from pydantic import BaseModel, ConfigDict

    schema = getattr(tool, "args_schema", None)
    if not isinstance(schema, type) or not issubclass(schema, BaseModel):
        return tool
    config = {**dict(schema.model_config), "extra": "forbid"}
    strict_schema = type(
        f"{schema.__name__}HarnestStrict",
        (schema,),
        {"model_config": ConfigDict(**config)},
    )
    allowed = frozenset(strict_schema.model_fields)
    object.__setattr__(tool, "args_schema", strict_schema)
    object.__setattr__(
        tool,
        "handle_validation_error",
        _langchain_validation_error(str(tool.name), allowed),
    )
    return tool


def _langchain_validation_error(tool_name: str, allowed: frozenset[str]) -> Any:
    """Build one value-free repair message for LangChain validation failures."""

    def message(error: Any) -> str:
        unknown = _extra_validation_fields(error)
        return (
            unknown_argument_error(tool_name, unknown, allowed)
            if unknown
            else invalid_argument_error(tool_name, allowed)
        )

    return message


def _extra_validation_fields(error: Any) -> tuple[str, ...]:
    """Extract only rejected field names, never validation input values."""

    entries = error.errors(include_input=False)
    return tuple(
        str(location[0])
        for item in entries
        if item.get("type") == "extra_forbidden"
        and isinstance((location := item.get("loc")), tuple)
        and location
    )


def _govern_langchain_base_tool(tool: Any) -> Any:
    """Apply universal lifecycle once to a native LangChain BaseTool."""

    async_operation = getattr(tool, "ainvoke", None)
    if not callable(async_operation) or _is_governed_mcp_operation(async_operation):
        return tool
    if getattr(async_operation, "__harnest_tool_lifecycle_wrapped__", False):
        return tool

    object.__setattr__(
        tool,
        "ainvoke",
        _wrapped_async_base_tool(tool, async_operation),
    )

    sync_operation = getattr(tool, "invoke", None)
    if callable(sync_operation):
        object.__setattr__(
            tool,
            "invoke",
            _wrapped_sync_base_tool(tool, sync_operation),
        )
    return tool


def _wrapped_async_base_tool(tool: Any, operation: Any) -> Any:
    """Build an async lifecycle wrapper that retains ToolCall output semantics."""

    async def invoke(tool_input: Any, config: Any = None, **kwargs: Any) -> Any:
        return await operation(tool_input, config=config, **kwargs)

    invoke.__name__ = str(getattr(tool, "name", type(tool).__name__))
    governed = wrap_lifecycle_tool(invoke)

    async def compatible(
        tool_input: Any, config: Any = None, **kwargs: Any
    ) -> Any:
        result = await governed(tool_input, config=config, **kwargs)
        return _native_tool_call_output(tool, tool_input, result)

    # Materialization checks this marker because a shared BaseTool may be
    # compiled more than once without acquiring another lifecycle wrapper.
    setattr(compatible, "__harnest_tool_lifecycle_wrapped__", True)
    return compatible


def _wrapped_sync_base_tool(tool: Any, operation: Any) -> Any:
    """Build a sync lifecycle wrapper that retains ToolCall output semantics."""

    def invoke(tool_input: Any, config: Any = None, **kwargs: Any) -> Any:
        return operation(tool_input, config=config, **kwargs)

    invoke.__name__ = str(getattr(tool, "name", type(tool).__name__))
    governed = wrap_lifecycle_tool(invoke)

    def compatible(tool_input: Any, config: Any = None, **kwargs: Any) -> Any:
        result = governed(tool_input, config=config, **kwargs)
        return _native_tool_call_output(tool, tool_input, result)

    setattr(compatible, "__harnest_tool_lifecycle_wrapped__", True)
    return compatible


def _native_tool_call_output(
    tool: Any,
    tool_input: Any,
    result: Any,
    *,
    status: str = "success",
) -> Any:
    """Restore LangChain's ToolMessage envelope after a lifecycle short-circuit."""

    if not isinstance(tool_input, Mapping) or tool_input.get("type") != "tool_call":
        return result
    call_id = tool_input.get("id")
    if not isinstance(call_id, str) or not call_id:
        return result
    from langchain_core.tools.base import _format_output

    # BaseTool normally formats this envelope after its coroutine returns.
    # Finish and post-hook replacement bypass that point, so the pinned
    # framework formatter remains authoritative for content normalization.
    formatted = _format_output(
        result,
        None,
        call_id,
        str(getattr(tool, "name", type(tool).__name__)),
        status,
    )
    return _restore_tool_message_identity(tool, formatted, call_id)


def _restore_tool_message_identity(tool: Any, result: Any, call_id: str) -> Any:
    """Keep lifecycle replacements attached to the model's original tool call."""

    if isinstance(result, list):
        return [
            _restore_tool_message_identity(tool, item, call_id)
            for item in result
        ]
    copier = getattr(result, "model_copy", None)
    if getattr(result, "type", None) != "tool" or not callable(copier):
        return result
    # Hooks may replace content and status, but moving a response to another
    # call would corrupt ToolNode correlation and downstream message history.
    return copier(
        update={
            "tool_call_id": call_id,
            "name": str(getattr(tool, "name", type(tool).__name__)),
        }
    )


def _build_ready_agent(
    definition: AgentDefinition,
    tools: Sequence[Any] = (),
    middleware: Sequence[Any] = (),
    *,
    graph_node: bool = False,
    consume_value: bool = False,
    checkpointer: Any = None,
) -> Any:
    """Build a simple LangChain tool-loop graph for an agent definition.

    MCP discovery is asynchronous and is therefore handled by the runtime
    lifecycle before this function is called. Provider-specific ADK features
    are rejected instead of being silently discarded.
    """

    if not isinstance(definition, AgentDefinition):
        raise TypeError("build_agent definition must be an AgentDefinition")
    if definition.subagents:
        raise ValueError(
            "LangGraph agent definitions cannot implicitly attach ADK subagents; "
            "place them explicitly in a Graph"
        )
    if definition.generate_content_config is not None:
        raise ValueError(
            "ADK generate_content_config is not supported by LangGraph models"
        )
    if definition.output_key is not None:
        raise ValueError("ADK output_key is not supported by LangGraph agents")

    try:
        from langchain.agents import create_agent
    except ImportError as exc:  # pragma: no cover - optional backend dependency
        raise RuntimeError(
            "building a LangGraph agent requires langchain"
        ) from exc

    model = _resolve_langchain_model(definition.model)
    kwargs = {
        "model": model,
        "tools": _agent_tools(definition, tools),
        "system_prompt": definition.instruction,
        "name": definition.name,
        "middleware": [
            _langgraph_agent_scope_middleware(definition.name),
            *(
                bind_model_extension(item, agent_name=definition.name)
                for item in middleware
            ),
        ],
    }
    if definition.output_schema is not None:
        # Passing the model class lets LangChain select provider-native output
        # when available and its tool strategy for other capable providers.
        kwargs["response_format"] = provider_output_schema(
            definition.output_schema
        )
    # Omitting the argument preserves LangGraph's own no-persistence default
    # for direct backend use; compiled Harnest roots always pass their authority.
    if checkpointer is not None:
        kwargs["checkpointer"] = checkpointer
    target = create_agent(**kwargs)
    target = _apply_history_projection(
        definition,
        target,
        graph_node=graph_node,
        consume_value=consume_value,
    )
    return propagate_litellm_lifecycles(model, target)


def _agent_tools(definition: AgentDefinition, discovered: Sequence[Any]) -> list[Any]:
    """Resolve authored tools; sandbox grants never add implicit model tools."""
    tools = _langchain_tools((*definition.tools, *discovered))
    assigned_sandboxes(definition)
    return tools


def _langgraph_agent_scope_middleware(agent_name: str) -> Any:
    """Bind the AgentDefinition identity around native model and tool calls."""

    from langchain.agents.middleware import AgentMiddleware

    class HarnestAgentScopeMiddleware(AgentMiddleware):
        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            with _managed_agent_scope(agent_name):
                return await handler(_project_principal_tools(request))

        def wrap_model_call(self, request: Any, handler: Any) -> Any:
            with _managed_agent_scope(agent_name):
                return handler(_project_principal_tools(request))

        async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
            with _managed_agent_scope(agent_name):
                _require_principal_tool(request)
                return await handler(request)

        def wrap_tool_call(self, request: Any, handler: Any) -> Any:
            with _managed_agent_scope(agent_name):
                _require_principal_tool(request)
                return handler(request)

    return HarnestAgentScopeMiddleware()


def _project_principal_tools(request: Any) -> Any:
    """Give one model call only tools available to its runtime principal."""

    from ..agent_principal import capability_is_available

    tools = getattr(request, "tools", None)
    if tools is None:
        return request
    selected = [tool for tool in tools if capability_is_available(tool)]
    return request if len(selected) == len(tools) else request.override(tools=selected)


def _require_principal_tool(request: Any) -> None:
    """Recheck execution so replayed calls cannot bypass model projection."""

    from ..agent_principal import require_capability

    tool = getattr(request, "tool", None)
    call = getattr(request, "tool_call", {})
    name = call.get("name") if isinstance(call, Mapping) else None
    if tool is not None:
        require_capability(tool, name=str(name or getattr(tool, "name", "tool")))


@contextmanager
def _managed_agent_scope(agent_name: str):
    """Derive nested identity while sharing the root's revocable capabilities."""

    from ..context import activate_context, context, derive_agent_context

    try:
        active = context.current()
    except RuntimeError:
        # Direct framework use has no Harnest capability authority to derive.
        yield
        return
    if active.agent_name == agent_name:
        yield
        return
    with activate_context(derive_agent_context(active, agent_name=agent_name)):
        yield


def _apply_history_projection(
    definition: AgentDefinition,
    target: Any,
    *,
    graph_node: bool,
    consume_value: bool,
) -> Any:
    """Apply portable history semantics before the native agent sees state."""

    if definition.history == "session" and not graph_node:
        return target
    try:
        from langchain_core.runnables import RunnableLambda, RunnableSequence
    except ImportError as exc:  # pragma: no cover - optional backend dependency
        raise RuntimeError(
            "building a LangGraph agent requires langchain-core"
        ) from exc

    def projection(state: Any, **_execution_options: Any) -> Any:
        # RunnableSequence forwards invocation options such as checkpoint
        # durability to its first step; history projection does not own them.
        projected = (
            _session_input(state)
            if definition.history == "session"
            else _turn_input(state)
        )
        return _append_node_input(
            projected,
            state,
            consume_value=consume_value,
            retain_history=definition.history == "session",
        )

    return RunnableSequence(
        RunnableLambda(projection),
        target,
        name=definition.name,
    )


def _session_input(state: Any) -> Any:
    if not isinstance(state, Mapping):
        return state
    projected = dict(state)
    projected.pop("_harnest_turn_start", None)
    return projected


def _turn_input(state: Any) -> Any:
    """Project one turn's input without replaying committed conversation state."""

    if not isinstance(state, Mapping):
        return state
    projected = _session_input(state)
    messages = state.get("messages")
    if not isinstance(messages, (list, tuple)):
        return projected
    start = state.get("_harnest_turn_start")
    if not isinstance(start, int) or isinstance(start, bool) or start < 0:
        start = max(len(messages) - 1, 0)
    projected["messages"] = list(messages[start:])
    return projected


def _append_node_input(
    projected: Any,
    state: Any,
    *,
    consume_value: bool,
    retain_history: bool,
) -> Any:
    """Promote a predecessor's portable output to the agent's user input."""

    if not consume_value or not isinstance(projected, Mapping):
        return projected
    if not isinstance(state, Mapping) or state.get("value") is None:
        return projected
    try:
        from langchain_core.messages import HumanMessage
    except ImportError as exc:  # pragma: no cover - optional backend dependency
        raise RuntimeError(
            "building a LangGraph agent requires langchain-core"
        ) from exc
    history = projected.get("messages", ()) if retain_history else ()
    if not isinstance(history, (list, tuple)):
        history = ()
    value = HumanMessage(content=_model_input_text(state["value"]))
    return {**projected, "messages": [*history, value]}


@dataclass(frozen=True, slots=True)
class ManagedAgentPlan:
    """Inert data awaiting asynchronous MCP discovery by the runtime driver."""

    definition: AgentDefinition
    tools: tuple[Any, ...] = ()
    middleware: tuple[Any, ...] = ()
    checkpointer: Any = None


@dataclass(frozen=True, slots=True)
class ManagedGraphPlan:
    """Inert graph awaiting MCP discovery for its managed agent nodes."""

    graph: Graph
    middleware: tuple[Any, ...] = ()
    checkpointer: Any = None

    @property
    def name(self) -> str:
        return self.graph.name


def materialize_agent(
    plan: ManagedAgentPlan, mcp_tools: Sequence[Any]
) -> Any:
    """Build the executable target after the runtime has discovered MCP tools."""

    if not isinstance(plan, ManagedAgentPlan):
        raise TypeError("materialize_agent expects a ManagedAgentPlan")
    definition = replace(plan.definition, mcp=())
    return _build_ready_agent(
        definition,
        (*plan.tools, *mcp_tools),
        plan.middleware,
        checkpointer=plan.checkpointer,
    )


def managed_graph_mcp_clients(
    plan: ManagedGraphPlan,
) -> tuple[tuple[Any, ...], ...]:
    """Return MCP client groups in deterministic graph traversal order."""

    if not isinstance(plan, ManagedGraphPlan):
        raise TypeError("managed_graph_mcp_clients expects a ManagedGraphPlan")
    groups: list[tuple[Any, ...]] = []

    def visit(graph: Graph) -> None:
        for value in graph.nodes.values():
            if isinstance(value, AgentDefinition) and value.mcp:
                groups.append(tuple(value.mcp))
            elif isinstance(value, Graph):
                visit(value)

    visit(plan.graph)
    return tuple(groups)


def materialize_graph(
    plan: ManagedGraphPlan,
    mcp_tool_groups: Sequence[Sequence[Any]],
) -> Any:
    """Build a graph after resolving each managed agent node's MCP tools."""

    if not isinstance(plan, ManagedGraphPlan):
        raise TypeError("materialize_graph expects a ManagedGraphPlan")
    groups = iter(mcp_tool_groups)

    def resolve(graph: Graph) -> Graph:
        nodes: dict[str, Any] = {}
        for name, value in graph.nodes.items():
            if isinstance(value, AgentDefinition) and value.mcp:
                try:
                    tools = tuple(next(groups))
                except StopIteration as exc:
                    raise ValueError("missing MCP tool group for graph agent") from exc
                nodes[name] = replace(
                    value, mcp=(), tools=(*value.tools, *tools)
                )
            elif isinstance(value, Graph):
                nodes[name] = resolve(value)
            else:
                nodes[name] = value
        return Graph(
            name=graph.name,
            nodes=nodes,
            edges=graph.edges,
            description=graph.description,
            max_concurrency=graph.max_concurrency,
            output_schema=graph.output_schema,
        )

    resolved = resolve(plan.graph)
    try:
        next(groups)
    except StopIteration:
        pass
    else:
        raise ValueError("received extra MCP tool groups for graph agents")
    return _build_ready_graph(
        resolved, plan.middleware, checkpointer=plan.checkpointer
    )


def build_agent(
    definition: AgentDefinition,
    tools: Sequence[Any] = (),
    middleware: Sequence[Any] = (),
    checkpointer: Any = None,
) -> Any:
    """Build a LangGraph agent, deferring MCP discovery until runtime."""

    if not isinstance(definition, AgentDefinition):
        raise TypeError("build_agent definition must be an AgentDefinition")
    if definition.mcp:
        return ManagedAgentPlan(
            definition, tuple(tools), tuple(middleware), checkpointer
        )
    return _build_ready_agent(
        definition, tools, middleware, checkpointer=checkpointer
    )


def _is_async_callable(value: Callable[..., Any]) -> bool:
    return inspect.iscoroutinefunction(value) or inspect.iscoroutinefunction(
        getattr(value, "__call__", None)
    )


_SESSION_STATE_KEY = "_harnest_state"


def _normalize_result(result: Any) -> dict[str, Any]:
    """Convert portable node events into LangGraph state updates."""

    if not isinstance(result, Event):
        return {"value": result, "route": None}

    update: dict[str, Any] = {"route": result.route}
    if result.output is not None:
        update["value"] = result.output
    if result.message is not None:
        try:
            from langchain_core.messages import AIMessage
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "emitting LangGraph messages requires langchain-core"
            ) from exc
        update["messages"] = [AIMessage(content=result.message)]
    if result.state_delta:
        update[_SESSION_STATE_KEY] = dict(result.state_delta)
    return update


def _callable_node(action: Callable[..., Any]) -> Callable[..., Any]:
    """Project node state without inheriting a same-named agent's grants."""

    if _is_async_callable(action):

        async def invoke(state: Mapping[str, Any]) -> dict[str, Any]:
            result = call_graph_node(
                action,
                state.get("value"),
                GraphContext(state.get(_SESSION_STATE_KEY, {})),
            )
            result = await result
            return _normalize_result(result)

    else:

        def invoke(state: Mapping[str, Any]) -> dict[str, Any]:
            result = call_graph_node(
                action,
                state.get("value"),
                GraphContext(state.get(_SESSION_STATE_KEY, {})),
            )
            if inspect.isawaitable(result):
                raise TypeError(
                    "graph node returned an awaitable; declare the callable with "
                    "'async def' so LangGraph can schedule it asynchronously"
                )
            return _normalize_result(result)

    invoke.__name__ = getattr(action, "__name__", type(action).__name__)
    from ..sandbox_graph_scope import deny_graph_callable

    return deny_graph_callable(invoke)


def _route_values_match(expected: Any, actual: Any) -> bool:
    expected_values = expected if isinstance(expected, tuple) else (expected,)
    actual_values = actual if isinstance(actual, tuple) else (actual,)
    return any(
        type(left) is type(right) and left == right
        for left in expected_values
        for right in actual_values
    )


def _last_write(_current: Any, update: Any) -> Any:
    """Allow parallel branches while preserving scalar neutral graph state."""

    return update


def _merge_state(current: Any, update: Any) -> dict[str, Any]:
    """Merge explicit state deltas so parallel branches retain both writes."""

    merged = dict(current) if isinstance(current, Mapping) else {}
    if isinstance(update, Mapping):
        merged.update(update)
    return merged


def _route_selector(edges: Sequence[Edge], end: str):
    """Resolve portable route labels to validated LangGraph destinations."""

    def select(state: Mapping[str, Any]) -> str | list[str]:
        route = state.get("route")
        targets = [
            edge.target
            for edge in edges
            if edge.route is None or _route_values_match(edge.route, route)
        ]
        if not targets:
            return end
        return targets[0] if len(targets) == 1 else targets

    return select


def _lower_node(
    value: Any,
    Pregel: type[Any],
    middleware: Sequence[Any],
    *,
    direct_input: bool,
) -> Any:
    """Lower one portable or explicitly native value into a graph node."""

    from ..sandbox_graph_scope import deny_langgraph_node

    if isinstance(value, AgentDefinition):
        if value.mcp:
            raise RuntimeError("managed MCP graph was lowered before materialization")
        return _build_ready_agent(
            value,
            middleware=middleware,
            graph_node=True,
            consume_value=direct_input,
        )
    if isinstance(value, Graph):
        return _build_ready_graph(value, middleware=middleware)
    if isinstance(value, _AdvancedAgentDefinition):
        return _advanced_graph_node(value, Pregel)
    if isinstance(value, Pregel):
        return deny_langgraph_node(passthrough_native(value))
    if isinstance(value, Join):
        return lambda _state: {}
    if callable(value):
        return _callable_node(value)
    raise TypeError(
        "LangGraph nodes must be AgentDefinition, Agent.advanced, callable, Graph, "
        f"Join, or Pregel values; got {type(value).__name__}"
    )


def _advanced_graph_node(value: _AdvancedAgentDefinition, Pregel: type[Any]) -> Any:
    """Prepare one compiled native LangGraph as a Harnest graph node."""

    # Root adapters operate on public invocation envelopes and would bypass the
    # state contract established by the parent graph if applied to a child node.
    if value.input_adapter is not None or value.output_adapter is not None:
        raise ValueError(
            "embedded Agent.advanced graph nodes cannot use root input/output adapters"
        )
    if not isinstance(value.target, Pregel):
        raise TypeError(
            "embedded Agent.advanced LangGraph nodes require a compiled Pregel target"
        )
    # Validate at composition time so malformed native graphs fail compilation,
    # before a deployed request can enter an incomplete execution topology.
    value.target.validate()
    from ..sandbox_graph_scope import deny_langgraph_node

    return deny_langgraph_node(passthrough_native(value.target))


def _graph_contains_agent(graph: Graph) -> bool:
    return any(
        isinstance(value, AgentDefinition)
        or (isinstance(value, Graph) and _graph_contains_agent(value))
        for value in graph.nodes.values()
    )


def _graph_contains_mcp_agent(graph: Graph) -> bool:
    """Detect whether runtime MCP materialization is needed at any graph depth."""

    return any(
        (isinstance(value, AgentDefinition) and bool(value.mcp))
        or (isinstance(value, Graph) and _graph_contains_mcp_agent(value))
        for value in graph.nodes.values()
    )


def build_graph(
    graph: Graph | Any,
    middleware: Sequence[Any] = (),
    checkpointer: Any = None,
) -> Any:
    """Compile a neutral graph, or validate and pass through a native Pregel."""

    END, LANGGRAPH_START, StateGraph, add_messages, Pregel = _langgraph_types()
    if isinstance(graph, Pregel):
        return passthrough_native(graph)
    if not isinstance(graph, Graph):
        raise TypeError("build_graph expects a Graph or native Pregel application")
    if middleware and not _graph_contains_agent(graph):
        raise ValueError(
            "LangGraph-native extensions require at least one Agent node"
        )
    if _graph_contains_mcp_agent(graph):
        return ManagedGraphPlan(graph, tuple(middleware), checkpointer)
    return _build_ready_graph(graph, middleware, checkpointer=checkpointer)


def _build_ready_graph(
    graph: Graph,
    middleware: Sequence[Any] = (),
    *,
    checkpointer: Any = None,
) -> Any:
    """Lower a graph whose agent nodes no longer require MCP discovery."""

    END, LANGGRAPH_START, StateGraph, add_messages, Pregel = _langgraph_types()

    builder = StateGraph(_graph_state(add_messages))
    runtime_nodes = []
    direct_inputs = {
        edge.target for edge in graph.edges if edge.source != START
    }
    for name, value in graph.nodes.items():
        runtime_node = _lower_node(
            value,
            Pregel,
            middleware,
            direct_input=name in direct_inputs,
        )
        runtime_nodes.append(runtime_node)
        builder.add_node(name, runtime_node)
    outgoing, join_inputs = _classified_edges(graph)
    _add_outgoing_edges(builder, outgoing, LANGGRAPH_START, END)
    _add_join_edges(builder, join_inputs, LANGGRAPH_START)
    _add_terminal_edges(builder, graph, END)

    projection_complete = _graph_agent_principal_projection_complete(graph, Pregel)
    compiled = builder.compile(name=graph.name, checkpointer=checkpointer)
    if graph.max_concurrency is not None:
        compiled = compiled.with_config(max_concurrency=graph.max_concurrency)
    # Native Pregel nodes own their internal model and tool middleware. Record
    # that boundary on the completed graph so the outer runtime can fail before
    # accepting authority it cannot project.
    object.__setattr__(
        compiled, _AGENT_PRINCIPAL_PROJECTION_COMPLETE, projection_complete
    )
    for runtime_node in runtime_nodes:
        propagate_litellm_lifecycles(runtime_node, compiled)
    return compiled


def _graph_agent_principal_projection_complete(
    graph: Graph, Pregel: type[Any] | None = None
) -> bool:
    """Return whether every model-bearing graph node is Harnest-managed."""

    native_type = Pregel
    if native_type is None:
        *_, native_type = _langgraph_types()
    for value in graph.nodes.values():
        if isinstance(value, Graph):
            if not _graph_agent_principal_projection_complete(value, native_type):
                return False
            continue
        if isinstance(value, (_AdvancedAgentDefinition, native_type)):
            return False
    return True


def _graph_state(add_messages: Any) -> type[Any]:
    # The functional form evaluates the locally imported reducer even though
    # this module uses postponed annotations.
    return TypedDict(
        "GraphState",
        {
            "messages": Annotated[list[Any], add_messages],
            "value": Annotated[Any, _last_write],
            "route": Annotated[Any, _last_write],
            "_harnest_turn_start": Annotated[int, _last_write],
            _SESSION_STATE_KEY: Annotated[dict[str, Any], _merge_state],
        },
        total=False,
    )


def _classified_edges(
    graph: Graph,
) -> tuple[dict[str, list[Edge]], dict[str, list[str]]]:
    """Classify portable edges before mutating the native graph builder."""

    outgoing: dict[str, list[Edge]] = defaultdict(list)
    join_inputs: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        if not isinstance(graph.nodes[edge.target], Join):
            outgoing[edge.source].append(edge)
            continue
        if edge.route is not None:
            raise ValueError(f"Join node {edge.target!r} cannot have routed incoming edges")
        join_inputs[edge.target].append(edge.source)
    return outgoing, join_inputs


def _add_outgoing_edges(
    builder: Any, outgoing: Mapping[str, list[Edge]], graph_start: str, end: str
) -> None:
    """Lower each portable edge group once to avoid competing native routes."""

    for source, edges in outgoing.items():
        native_source = graph_start if source == START else source
        if all(edge.route is None for edge in edges):
            for edge in edges:
                builder.add_edge(native_source, edge.target)
        else:
            builder.add_conditional_edges(native_source, _route_selector(edges, end))


def _add_join_edges(
    builder: Any, join_inputs: Mapping[str, list[str]], graph_start: str
) -> None:
    for target, sources in join_inputs.items():
        if START in sources and len(sources) > 1:
            raise ValueError(f"Join node {target!r} cannot combine START with node inputs")
        normalized = [graph_start if source == START else source for source in sources]
        builder.add_edge(normalized if len(normalized) > 1 else normalized[0], target)


def _add_terminal_edges(builder: Any, graph: Graph, end: str) -> None:
    all_sources = {edge.source for edge in graph.edges}
    for node_name in graph.nodes:
        if node_name not in all_sources:
            builder.add_edge(node_name, end)


lower_graph = build_graph
lower_agent = build_agent

__all__ = [
    "ManagedAgentPlan",
    "ManagedGraphPlan",
    "build_agent",
    "build_graph",
    "lower_agent",
    "lower_graph",
    "managed_graph_mcp_clients",
    "materialize_agent",
    "materialize_graph",
    "passthrough_native",
]
