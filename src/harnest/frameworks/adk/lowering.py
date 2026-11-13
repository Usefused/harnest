"""Lower provider-neutral Harnest graphs to Google ADK workflows."""

from __future__ import annotations

import functools
import inspect
from collections.abc import AsyncGenerator, Generator, Mapping
from typing import Any

from pydantic import BaseModel

from harnest.agent import AgentDefinition, Tool, _AdvancedAgentDefinition
from harnest.durable import is_durable_tool
from harnest.frameworks.adk.durable import adk_durable_tool
from harnest.graph import START, Event, Graph, GraphContext, Join, call_graph_node
from harnest.model_lifecycle import propagate_litellm_lifecycles
from harnest.mcp_lifecycle import propagate_mcp_lifecycles
from harnest.model import resolve_model
from ..context import framework_scope
from harnest.structured import PydanticModel, provider_output_schema
from harnest.sandbox_graph_scope import deny_adk_node


def _adk_route(route: Any) -> Any:
    return list(route) if isinstance(route, tuple) else route


def _adk_event(value: Event) -> Any:
    """Preserve authored state provenance separately from ADK's internal actions."""

    from google.adk.events import Event as AdkEvent
    from google.adk.events import EventActions
    from google.genai import types

    kwargs: dict[str, Any] = {}
    if value.output is not None:
        kwargs["output"] = value.output
    if value.route is not None:
        # ADK's Event constructor accepts route as a convenience field and
        # stores it in EventActions.
        kwargs["route"] = _adk_route(value.route)
    if value.message is not None:
        kwargs["content"] = types.Content(
            role="model", parts=[types.Part(text=value.message)]
        )
    if value.state_delta:
        kwargs["actions"] = EventActions(state_delta=dict(value.state_delta))
        # ADK adds private bookkeeping to actions.state_delta. Only this
        # explicitly authored copy is safe to project into public UI events.
        kwargs["custom_metadata"] = {"harnest.state_delta": dict(value.state_delta)}
    return AdkEvent(**kwargs)


def _lower_callable_result(value: Any) -> Any:
    return _adk_event(value) if isinstance(value, Event) else value


def _neutral_callable_input(value: Any) -> Any:
    """Convert ADK's root Content input to the graph API's text value."""

    parts = getattr(value, "parts", None)
    if parts is None:
        return value
    text_value = "".join(
        part.text
        for part in parts
        if isinstance(getattr(part, "text", None), str)
    )
    return text_value if text_value else value


def _callable_adapter(value: Any, *, name: str) -> Any:
    """Adapt a neutral single-input callable to an ADK FunctionNode."""

    async def invoke(ctx: Any, node_input: Any) -> AsyncGenerator[Any, None]:
        result = call_graph_node(
            value,
            _neutral_callable_input(node_input),
            # ADK exposes a mapping-like State proxy that intentionally does
            # not inherit Mapping; copy it into the neutral immutable view.
            GraphContext(ctx.state.to_dict()),
        )
        if inspect.isawaitable(result):
            result = await result
        if inspect.isasyncgen(result):
            async for item in result:
                yield _lower_callable_result(item)
            return
        if isinstance(result, Generator):
            for item in result:
                yield _lower_callable_result(item)
            return
        if result is not None:
            yield _lower_callable_result(result)

    invoke.__name__ = name
    invoke.__doc__ = inspect.getdoc(value)
    return invoke


def _own_node_lifecycles(workflow: Any, native_nodes: dict[str, Any]) -> Any:
    """Make the enclosing workflow own every lowered node lifecycle."""

    for native in native_nodes.values():
        propagate_litellm_lifecycles(native, workflow)
        propagate_mcp_lifecycles(native, workflow)
    return workflow


def lower_graph(graph: Graph) -> Any:
    """Build a validated native ``google.adk.workflow.Workflow``."""

    if not isinstance(graph, Graph):
        raise TypeError("ADK graph backend expects a harnest.graph.Graph")
    return _lower_graph(graph, active=set())


def _lower_graph(graph: Graph, *, active: set[int]) -> Any:
    """Recursively lower one portable graph into an ADK workflow."""

    from google.adk.workflow import Edge as AdkEdge
    from google.adk.workflow import JoinNode
    from google.adk.workflow import START as ADK_START
    from google.adk.workflow import Workflow
    from google.adk.workflow import BaseNode
    from google.adk.workflow import node as adk_node

    identity = id(graph)
    if identity in active:
        raise ValueError(f"nested graph cycle detected at {graph.name!r}")
    active.add(identity)
    try:
        native_nodes: dict[str, BaseNode] = {}
        for name, value in graph.nodes.items():
            value = _embedded_graph_node(value)
            if isinstance(value, Join):
                native = JoinNode(name=name)
            elif isinstance(value, Graph):
                nested = _lower_graph(value, active=active)
                native = adk_node(nested, name=name)
                propagate_litellm_lifecycles(nested, native)
                propagate_mcp_lifecycles(nested, native)
            elif isinstance(value, AgentDefinition):
                built_agent = _build_agent_node(
                    value,
                    direct_input=_receives_direct_input(graph, name),
                )
                native = adk_node(built_agent, name=name)
                propagate_litellm_lifecycles(built_agent, native)
                propagate_mcp_lifecycles(built_agent, native)
            elif callable(value):
                native = deny_adk_node(adk_node(
                    _callable_adapter(value, name=name),
                    name=name,
                ))
            elif isinstance(value, BaseNode):
                native = deny_adk_node(adk_node(value, name=name))
            else:
                raise TypeError(
                    f"graph node {name!r} expected AgentDefinition, Agent.advanced, "
                    "callable, Graph, Join, or native ADK BaseNode/BaseAgent; got "
                    f"{type(value).__name__}"
                )
            native_nodes[name] = native

        native_edges = [
            AdkEdge(
                from_node=ADK_START
                if edge.source == START
                else native_nodes[edge.source],
                to_node=native_nodes[edge.target],
                route=_adk_route(edge.route),
            )
            for edge in graph.edges
        ]
        return _own_node_lifecycles(
            Workflow(
                name=graph.name,
                description=graph.description,
                edges=native_edges,
                max_concurrency=graph.max_concurrency,
            ),
            native_nodes,
        )
    finally:
        active.remove(identity)


def _embedded_graph_node(value: Any) -> Any:
    """Normalize an explicitly advanced wrapper for graph-node lowering."""

    if isinstance(value, _AdvancedAgentDefinition):
        return _advanced_graph_node(value)
    return value


def _receives_direct_input(graph: Graph, node_name: str) -> bool:
    """Distinguish graph entry input from a preceding node's output."""

    return any(
        edge.target == node_name and edge.source != START for edge in graph.edges
    )


def _build_agent_node(
    definition: AgentDefinition, *, direct_input: bool
) -> Any:
    """Select ADK's workflow-only dispatch mode for a managed agent node."""

    built = build_agent(definition)
    if definition.history == "turn":
        return _copy_agent_mode(
            built,
            mode="single_turn",
            include_contents="none",
        )
    if not direct_input:
        return built
    # ADK chat nodes reject non-START edges. Single-turn dispatch injects the
    # direct node value, while explicit content inclusion retains this node's
    # prior session events across invocations.
    return _copy_agent_mode(
        built,
        mode="single_turn",
        include_contents="default",
    )


def _copy_agent_mode(
    built: Any, *, mode: str, include_contents: str
) -> Any:
    """Copy an ADK agent mode without dropping Harnest lifecycle ownership."""

    copied = built.model_copy(
        update={"mode": mode, "include_contents": include_contents}
    )
    # Pydantic copies declared fields, while Harnest lifecycle ownership is
    # intentionally attached out of band to avoid framework schema coupling.
    propagate_litellm_lifecycles(built, copied)
    propagate_mcp_lifecycles(built, copied)
    return copied


def _advanced_graph_node(value: _AdvancedAgentDefinition) -> Any:
    """Validate and unwrap one native ADK node embedded in a Harnest graph."""

    # Root adapters translate neutral request and response envelopes; a graph
    # node instead exchanges state according to its surrounding graph edges.
    if value.input_adapter is not None or value.output_adapter is not None:
        raise ValueError(
            "embedded Agent.advanced graph nodes cannot use root input/output adapters"
        )
    from google.adk.apps import App
    from google.adk.workflow import BaseNode

    # App-level plugins and services cannot be preserved inside another graph's
    # application boundary, so silently extracting root_agent would be unsafe.
    if isinstance(value.target, App):
        raise TypeError(
            "embedded Agent.advanced ADK graph nodes require a BaseNode target; "
            "ADK App targets are root-only"
        )
    target = value.target
    if not isinstance(target, BaseNode):
        raise TypeError(
            "embedded Agent.advanced ADK graph nodes require a BaseNode target"
        )
    return target


__all__ = ["lower_graph"]


@framework_scope("adk")
def build_agent(definition: AgentDefinition) -> Any:
    """Lower one managed agent definition to ADK's ``LlmAgent``.

    ADK is imported lazily so config generation and validation do not need
    model/runtime dependencies loaded. Sandbox agents retain native flows
    with a scoped consumed-code continuation compatibility adapter.
    """

    if definition.instruction is None:
        raise ValueError(
            "agent instruction is unresolved; provide instruction or use "
            "bundle_agent(__file__, agent) with a sibling instructions.md"
        )

    try:
        from google.adk.agents import LlmAgent
    except ImportError as exc:  # pragma: no cover - depends on optional runtime
        raise RuntimeError(
            "google-adk is required to build an agent; install the harnest package"
        ) from exc

    kwargs = _agent_kwargs(definition)
    from .agent_scope import managed_adk_agent_type

    native_type = managed_adk_agent_type(LlmAgent)
    built = native_type(**kwargs)
    propagate_litellm_lifecycles(kwargs["model"], built)
    propagate_mcp_lifecycles(kwargs["tools"], built)
    for child in kwargs["sub_agents"]:
        propagate_litellm_lifecycles(child, built)
        propagate_mcp_lifecycles(child, built)
    return built

def _agent_kwargs(definition: AgentDefinition) -> dict[str, Any]:
    """Translate the portable definition into ADK constructor arguments."""

    from .mcp_tools import build_toolset

    children = [_build_adk_subagent(child) for child in definition.subagents]
    runtime_tools = [
        *(_adk_runtime_tool(tool) for tool in definition.tools),
        *(build_toolset(client) for client in definition.mcp),
    ]
    from ...sandbox_assignments import assigned_sandboxes
    from .tokens import wrap_adk_model

    # Named declarations are capabilities for authored tools, never an
    # implicit model-facing execute-code tool or native code executor.
    assigned_sandboxes(definition)
    kwargs: dict[str, Any] = {
        "name": definition.name,
        "model": wrap_adk_model(resolve_model(definition.model), definition.token_policy, definition.name),
        "instruction": definition.instruction,
        "description": definition.description,
        "tools": runtime_tools,
        "sub_agents": children,
        # ADK rejects single_turn at an application root. Excluding prior
        # contents preserves the portable turn-only contract there; the
        # graph backend selects single_turn when this agent is a node.
        "mode": "chat",
        "include_contents": "default"
        if definition.history == "session"
        else "none",
    }
    if definition.skill_selection is not None:
        from .skill_selection import adk_selection_callback
        kwargs["before_model_callback"] = adk_selection_callback(definition.skill_selection)
    if definition.output_key is not None:
        kwargs["output_key"] = definition.output_key
    kwargs.update(_schema_kwargs(definition))
    if definition.generate_content_config is not None:
        kwargs["generate_content_config"] = _content_config(definition)
    return kwargs

def _schema_kwargs(definition: AgentDefinition) -> dict[str, PydanticModel]:
    """Expose only provider-owned output fields to the ADK node boundary.

    Harnest validates portable request models before framework lowering.
    Passing that schema to ADK would make ADK concatenate multipart content
    into text and reject otherwise-valid media references before its model
    callback can materialize them.
    """

    return {
        name: schema
        for name, schema in (
            (
                "output_schema",
                provider_output_schema(definition.output_schema)
                if definition.output_schema is not None
                else None,
            ),
        )
        if schema is not None
    }

def _content_config(definition: AgentDefinition) -> Any:
    """Translate authored mappings while preserving native ADK configuration."""

    config = definition.generate_content_config
    if not isinstance(config, Mapping):
        return config
    try:
        from google.genai import types
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "google-genai is required for generate_content_config"
        ) from exc
    return types.GenerateContentConfig(**dict(config))


def _adk_runtime_tool(tool: Tool) -> Tool:
    """Serialize validated Pydantic tool values before ADK wraps responses."""

    if not callable(tool):
        return tool
    runtime_tool = _adk_serializing_tool(tool)
    return adk_durable_tool(runtime_tool) if is_durable_tool(tool) else runtime_tool


def _adk_serializing_tool(tool: Tool) -> Tool:
    """Build the value adapter that executes inside any durable ADK boundary."""

    if getattr(tool, "__harnest_output_schema__", None) is None:
        return tool
    if inspect.iscoroutinefunction(tool):

        @functools.wraps(tool)
        async def async_call(*args: Any, **kwargs: Any) -> Any:
            return _adk_tool_result(await tool(*args, **kwargs))

        return async_call

    @functools.wraps(tool)
    def sync_call(*args: Any, **kwargs: Any) -> Any:
        return _adk_tool_result(tool(*args, **kwargs))

    return sync_call


def _adk_tool_result(value: Any) -> Any:
    """Avoid ADK's generic ``result`` envelope for Pydantic model values."""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    return value


def _build_adk_subagent(value: Any) -> Any:
    """Build or unwrap one value for ADK's native subagent collection."""

    if isinstance(value, AgentDefinition):
        return build_agent(value)
    from .agent_scope import scope_native_adk_agent

    if not isinstance(value, _AdvancedAgentDefinition):
        return scope_native_adk_agent(value)
    # Input and output adapters transform Harnest's root transport boundary;
    # applying them inside a parent would silently change the component contract.
    if value.input_adapter is not None or value.output_adapter is not None:
        raise ValueError(
            "embedded Agent.advanced subagents cannot use root input/output adapters"
        )
    try:
        from google.adk.agents import BaseAgent
        from google.adk.apps import App
    except ImportError as exc:  # pragma: no cover - optional runtime
        raise RuntimeError("advanced ADK subagents require google-adk") from exc
    # An App owns plugins and application services that cannot be preserved when
    # it is inserted into another application's delegation hierarchy.
    if isinstance(value.target, App):
        raise TypeError(
            "embedded Agent.advanced ADK subagents require a BaseAgent target; "
            "ADK App targets are root-only"
        )
    target = value.target
    if not isinstance(target, BaseAgent):
        raise TypeError(
            "embedded Agent.advanced ADK subagents require a BaseAgent target"
        )
    # ADK routes delegation by the native agent name. Accepting a different
    # wrapper identity would make discovery and runtime routing disagree.
    if value.name is not None and value.name != target.name:
        raise ValueError(
            "embedded Agent.advanced subagent name must match its native ADK "
            f"agent name {target.name!r}; got {value.name!r}"
        )
    return scope_native_adk_agent(target)
