"""Walk a managed agent tree: definitions, graphs, subagents, and native agents."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


def walk_agent_tree(root: Any) -> Iterator[Any]:
    """Yield each node of an agent tree once, unwrapping advanced definitions.

    Nodes are ``AgentDefinition`` and ``Graph`` values plus any native agent
    object that exposes ``run_async`` and ``sub_agents``.
    """

    from .agent import AgentDefinition, _AdvancedAgentDefinition
    from .graph import Graph

    seen: set[int] = set()
    pending = [root]
    while pending:
        value = pending.pop()
        # Wrappers and direct references share the same target identity.
        if isinstance(value, _AdvancedAgentDefinition):
            value = value.target
        if id(value) in seen:
            continue
        seen.add(id(value))
        yield value
        if isinstance(value, AgentDefinition):
            pending.extend(value.subagents)
        elif isinstance(value, Graph):
            pending.extend(value.nodes.values())
        elif callable(getattr(value, "run_async", None)):
            pending.extend(getattr(value, "sub_agents", ()))
