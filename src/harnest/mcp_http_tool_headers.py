"""Validate and encode schema-declared MCP routing headers without credential access."""

import re
from typing import Any

from .mcp_http_transport import header_value
from .mcp_resources import MCPResourceError

_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")


def header_paths(schema: dict[str, Any], path: tuple[str, ...] = ()) -> list[tuple[str, tuple[str, ...], str]]:
    """Accept annotations only along statically reachable object property paths."""

    found = []
    for key, value in schema.items():
        if key == "properties" and isinstance(value, dict):
            for name, child in value.items():
                found.extend(_property_headers(child, (*path, name)))
        elif key != "x-mcp-header" and _contains_annotation(value):
            raise MCPResourceError("MCP tool has an invalid header annotation path")
    return found


def _property_headers(schema: Any, path: tuple[str, ...]) -> list[tuple[str, tuple[str, ...], str]]:
    """Reject malformed header names and non-primitive annotated properties."""

    if not isinstance(schema, dict):
        return []
    found = header_paths(schema, path)
    if "x-mcp-header" in schema:
        name, kind = schema["x-mcp-header"], schema.get("type")
        if not isinstance(name, str) or not _TOKEN.fullmatch(name):
            raise MCPResourceError("MCP tool has an invalid header annotation name")
        if kind not in ("string", "integer", "boolean"):
            raise MCPResourceError("MCP tool has an invalid header annotation type")
        found.append((name, path, kind))
    return found


def _contains_annotation(value: Any) -> bool:
    """Detect annotations hidden behind arrays, compositions, or references."""

    if isinstance(value, dict):
        return "x-mcp-header" in value or any(_contains_annotation(child) for child in value.values())
    return isinstance(value, list) and any(_contains_annotation(child) for child in value)


def validated_paths(schema: dict[str, Any]) -> list[tuple[str, tuple[str, ...], str]]:
    """Reject duplicate case-insensitive header names and root annotations."""

    if "x-mcp-header" in schema:
        raise MCPResourceError("MCP tool has a root header annotation")
    paths = header_paths(schema)
    if len({name.lower() for name, _, _ in paths}) != len(paths):
        raise MCPResourceError("MCP tool has duplicate header annotations")
    return paths


def valid_tool_schema(schema: dict[str, Any]) -> bool:
    """Exclude invalid remote schemas before an agent can select them."""

    try:
        validated_paths(schema)
        return True
    except MCPResourceError:
        return False


def tool_headers(schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, str]:
    """Mirror declared primitives; absent or null fields omit the header."""

    headers = {}
    for name, path, kind in validated_paths(schema):
        value: Any = arguments
        for part in path:
            value = value.get(part) if isinstance(value, dict) else None
        if value is not None:
            headers["Mcp-Param-" + name] = _primitive_value(value, kind)
    return headers


def _primitive_value(value: Any, kind: str) -> str:
    """Use exact JSON primitive types and interoperable integer ranges."""

    expected = {"string": str, "integer": int, "boolean": bool}[kind]
    if type(value) is not expected:
        raise MCPResourceError("MCP routing parameter has the wrong type")
    if kind == "integer" and abs(value) > 2**53 - 1:
        raise MCPResourceError("MCP routing integer exceeds the safe range")
    text = str(value).lower() if kind == "boolean" else str(value)
    return header_value(text)
