"""Validate explicit sandbox grants and protect their framework tool names."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from .sandbox import Sandbox


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,46}\Z")


def validate_sandbox_name(name: str) -> None:
    """Keep authored sandbox identifiers bounded and consistent with filenames."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError(
            "sandbox name must be 1–47 ASCII letters, numbers, or underscores, "
            "starting with a letter or underscore; use the Python filename without .py"
        )


def assigned_sandboxes(definition: Any) -> tuple[tuple[str, Sandbox], ...]:
    """Fail closed when authored grants have not been resolved by compilation."""
    names = tuple(definition.sandboxes)
    bindings = definition._sandbox_bindings
    if set(names) != set(bindings):
        raise ValueError(
            f"agent {definition.name!r} has unresolved sandbox assignments; "
            "compile the managed service to resolve Agent(sandboxes=[...]) "
            "against the sandbox/ folder before building it"
        )
    return tuple((name, bindings[name]) for name in names)


def reject_sandbox_tool_collisions(tools: Sequence[Any], names: Sequence[str]) -> None:
    """Prevent authored or discovered tools from replacing sandbox authority."""
    reserved = frozenset(names)
    for tool in tools:
        conflicts = _tool_names(tool) & reserved
        if conflicts:
            name = sorted(conflicts)[0]
            raise ValueError(
                f"tool name {name!r} is reserved by the sandbox assignment; "
                "rename the other tool or change its MCP prefix"
            )


def _tool_names(tool: Any) -> set[str]:
    """Read public names from native tools, callables, and model tool mappings."""
    if not isinstance(tool, Mapping):
        return {getattr(tool, "name", ""), getattr(tool, "__name__", "")}
    function = tool.get("function", {})
    nested = function.get("name", "") if isinstance(function, Mapping) else ""
    return {tool.get("name", ""), nested}
