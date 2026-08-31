"""Lower provider-neutral Harnest graphs to Google ADK workflows."""

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator, Generator
from typing import Any

from harnest.agent import AgentDefinition, _AdvancedAgentDefinition
from harnest.graph import START, Event, Graph, GraphContext, Join, call_graph_node
from harnest.model_lifecycle import propagate_litellm_lifecycles
from harnest.mcp_lifecycle import propagate_mcp_lifecycles


def _adk_route(route: Any) -> Any:
    return list(route) if isinstance(route, tuple) else route


def _adk_event(value: Event) -> Any:
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
                native = adk_node(
                    _callable_adapter(value, name=name),
                    name=name,
                )
            elif isinstance(value, BaseNode):
                native = adk_node(value, name=name)
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

    built = definition.build()
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
