"""Provider-neutral graph definitions for deterministic agent workflows."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Sequence


START = "START"
"""The reserved source reference for graph entry edges."""

RouteValue = bool | int | str
Route = RouteValue | tuple[RouteValue, ...] | None


def _normalize_route(value: Any, *, field_name: str) -> Route:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        route = tuple(value)
        if not route:
            raise ValueError(f"{field_name} cannot be an empty sequence")
        if any(not isinstance(item, (bool, int, str)) for item in route):
            raise TypeError(
                f"{field_name} values must be booleans, integers, or strings"
            )
        if len(set(route)) != len(route):
            raise ValueError(f"{field_name} cannot contain duplicate values")
        return route
    raise TypeError(
        f"{field_name} must be a boolean, integer, string, sequence, or None"
    )


@dataclass(frozen=True, slots=True)
class Edge:
    """A directed graph edge between two node references."""

    source: str
    target: str
    route: Route = None

    def __post_init__(self) -> None:
        for field_name in ("source", "target"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise TypeError(f"edge {field_name} must be a non-empty string")
        object.__setattr__(
            self, "route", _normalize_route(self.route, field_name="edge route")
        )


@dataclass(frozen=True, slots=True)
class Join:
    """Marker for a node that joins all of its incoming graph branches."""


@dataclass(frozen=True, slots=True)
class Event:
    """Provider-neutral output emitted by a callable graph node.

    ``output`` is passed to downstream nodes, ``route`` selects conditional
    edges, and ``message`` emits user-facing assistant text. A node may set
    more than one field on the same event.
    """

    output: Any | None = None
    route: Route = None
    message: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "route", _normalize_route(self.route, field_name="event route")
        )
        if self.message is not None and not isinstance(self.message, str):
            raise TypeError("event message must be a string or None")
        if self.output is None and self.route is None and self.message is None:
            raise ValueError("event must provide output, route, or message")


@dataclass(frozen=True, slots=True)
class Graph:
    """A small, provider-neutral directed workflow graph.

    Node mapping keys are the stable references used by ``Edge``. Values are
    lowered by a backend and may be agent definitions, callables, nested
    graphs, joins, or backend-native nodes.
    """

    name: str
    nodes: Mapping[str, Any]
    edges: Sequence[Edge]
    description: str = ""
    max_concurrency: int | None = None
    _node_names: frozenset[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.isidentifier():
            raise ValueError("graph name must be a valid Python identifier")
        if self.name == "user":
            raise ValueError("graph name cannot be 'user'")
        if not isinstance(self.description, str):
            raise TypeError("graph description must be a string")
        if self.max_concurrency is not None and (
            isinstance(self.max_concurrency, bool)
            or not isinstance(self.max_concurrency, int)
            or self.max_concurrency < 1
        ):
            raise ValueError("graph max_concurrency must be a positive integer")
        if not isinstance(self.nodes, Mapping):
            raise TypeError("graph nodes must be a mapping")

        normalized_nodes = dict(self.nodes)
        if not normalized_nodes:
            raise ValueError("graph must define at least one node")
        for name in normalized_nodes:
            if not isinstance(name, str) or not name.isidentifier():
                raise ValueError(
                    f"graph node reference {name!r} must be a Python identifier"
                )
            if name in {START, "__START__"}:
                raise ValueError(f"graph node reference {name!r} is reserved")

        if isinstance(self.edges, (str, bytes)):
            raise TypeError("graph edges must be a sequence of Edge values")
        normalized_edges = tuple(self.edges)
        if any(not isinstance(edge, Edge) for edge in normalized_edges):
            raise TypeError("graph edges must contain only Edge values")

        node_names = frozenset(normalized_nodes)
        seen_edges: set[tuple[str, str]] = set()
        adjacency: dict[str, set[str]] = {
            START: set(), **{name: set() for name in node_names}
        }
        for edge in normalized_edges:
            if edge.source != START and edge.source not in node_names:
                raise ValueError(
                    f"graph edge references unknown source node {edge.source!r}"
                )
            if edge.target not in node_names:
                raise ValueError(
                    f"graph edge references unknown target node {edge.target!r}"
                )
            identity = (edge.source, edge.target)
            if identity in seen_edges:
                raise ValueError(
                    "graph contains duplicate edge "
                    f"{edge.source!r} -> {edge.target!r}"
                )
            seen_edges.add(identity)
            adjacency[edge.source].add(edge.target)

        reachable: set[str] = set()
        pending = [START]
        while pending:
            current = pending.pop()
            for target in adjacency[current]:
                if target not in reachable:
                    reachable.add(target)
                    pending.append(target)
        unreachable = sorted(node_names - reachable)
        if unreachable:
            raise ValueError(
                "graph contains nodes unreachable from START: "
                + ", ".join(repr(name) for name in unreachable)
            )

        object.__setattr__(self, "nodes", MappingProxyType(normalized_nodes))
        object.__setattr__(self, "edges", normalized_edges)
        object.__setattr__(self, "_node_names", node_names)

    def build(self, backend: str = "adk") -> Any:
        """Lower this graph with the named runtime backend."""

        if not isinstance(backend, str) or not backend.isidentifier():
            raise ValueError("graph backend must be a Python identifier")
        try:
            module = importlib.import_module(f"harnest.backends.{backend}")
        except ModuleNotFoundError as exc:
            if exc.name == f"harnest.backends.{backend}":
                raise ValueError(f"unknown graph backend: {backend}") from exc
            raise
        lower_graph = getattr(module, "lower_graph", None)
        if not callable(lower_graph):
            raise RuntimeError(
                f"graph backend {backend!r} does not export lower_graph"
            )
        return lower_graph(self)


__all__ = ["START", "Edge", "Event", "Graph", "Join"]
