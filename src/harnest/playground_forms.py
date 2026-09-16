"""Small source-preserving form edits for statically authored constructors."""

from __future__ import annotations

import ast
from typing import Any

from fastapi import HTTPException

_FIELDS = {"Agent": {"name", "description", "model", "history", "output_key"}, "Graph": {"description", "max_concurrency"}, "MCPClient": {"tools"}}


def constructor(text: str, line: int) -> tuple[ast.Call, str]:
    """Resolve only a known call at its current line, never evaluate a Python expression."""

    tree = ast.parse(text)
    functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.lineno == line]
    nodes = ast.walk(functions[0]) if functions else ast.walk(tree)
    for node in nodes:
        if not isinstance(node, ast.Call) or (not functions and node.lineno != line):
            continue
        kind = _constructor_kind(node)
        if kind:
            return node, kind
    raise HTTPException(422, "This declaration needs source editing; reopen it if its lines changed")


def _constructor_kind(node: ast.Call) -> str | None:
    """Exclude unpacked keyword dictionaries which could override form-owned values."""

    if any(keyword.arg is None for keyword in node.keywords):
        return None
    name = ast.unparse(node.func)
    return next((item for item in _FIELDS if name == item or name.startswith(item + ".")), None)


def form_description(text: str, line: int) -> dict[str, Any]:
    """Offer fields only when their current values are literal and round-trip safely."""

    call, kind = constructor(text, line)
    fields = {}
    for keyword in call.keywords:
        if keyword.arg not in _FIELDS[kind]:
            continue
        try:
            fields[keyword.arg] = ast.literal_eval(keyword.value)
        except (ValueError, TypeError):
            pass
    if kind == "MCPClient" and "tools" not in {keyword.arg for keyword in call.keywords}:
        fields["tools"] = None
    value = {"kind": kind, "fields": fields}
    if kind == "Graph":
        value["workflow"] = _workflow(call)
    return value


def _workflow(call: ast.Call) -> dict[str, Any] | None:
    """Expose literal graph topology while leaving inline/dynamic wiring in source."""

    values = {item.arg: item.value for item in call.keywords}
    try:
        nodes = values["nodes"]
        edges = values["edges"]
        if not isinstance(nodes, ast.Dict) or not isinstance(edges, (ast.List, ast.Tuple)):
            return None
        references = {ast.literal_eval(key): ast.unparse(value) for key, value in zip(nodes.keys, nodes.values)}
        if not all(_reference(value) for value in references.values()):
            return None
        return {"nodes": references, "edges": [_edge(value) for value in edges.elts]}
    except (KeyError, ValueError, TypeError):
        return None


def _edge(node: ast.expr) -> dict[str, Any]:
    """Decode explicit Edge calls including the START sentinel and conditional routes."""

    if not isinstance(node, ast.Call) or ast.unparse(node.func) != "Edge":
        raise ValueError("dynamic edge")
    values = dict(zip(("source", "target", "route"), node.args))
    values.update({item.arg: item.value for item in node.keywords})
    result = {}
    for key, value in values.items():
        result[key] = "START" if isinstance(value, ast.Name) and value.id == "START" else ast.literal_eval(value)
    if not {"source", "target"} <= result.keys() or result.keys() - {"source", "target", "route"}:
        raise ValueError("unsupported edge")
    return result


def _reference(value: str) -> bool:
    """Allow dotted names, never calls or executable expressions in node pickers."""

    return isinstance(value, str) and all(part.isidentifier() and not part.startswith("__") for part in value.split("."))


def patch_form(text: str, line: int, fields: dict[str, Any], workflow: dict[str, Any] | None) -> str:
    """Replace only reviewed keyword spans, preserving surrounding authored code and comments."""

    call, kind = constructor(text, line)
    available = form_description(text, line)["fields"]
    if fields.keys() - available.keys():
        raise HTTPException(422, "Dynamic or unsupported fields must be edited in source")
    replacements = {key: repr(value) for key, value in fields.items()}
    if workflow is not None:
        if kind != "Graph" or _workflow(call) is None:
            raise HTTPException(422, "Dynamic workflow must be edited in source")
        replacements.update(_workflow_expressions(workflow))
    return _replace_keywords(text, call, replacements)


def _workflow_expressions(workflow: dict[str, Any]) -> dict[str, str]:
    """Validate graph references and edge endpoints before generating constructor arguments."""

    from .graph import Edge

    nodes, edges = workflow.get("nodes", {}), workflow.get("edges", [])
    if not _valid_nodes(nodes):
        raise HTTPException(422, "Workflow nodes require names and Python symbol references")
    for value in edges:
        edge = Edge(**value)
        if edge.source not in {"START", *nodes} or edge.target not in nodes:
            raise HTTPException(422, "Each connection must refer to an existing workflow node")
    references = ", ".join(f"{key!r}: {value}" for key, value in nodes.items())
    connections = ", ".join(f"Edge({item['source']!r}, {item['target']!r}, route={item.get('route')!r})" for item in edges)
    return {"nodes": "{" + references + "}", "edges": "[" + connections + "]"}


def _valid_nodes(nodes: Any) -> bool:
    """Check row-derived node mappings before using their symbols in generated Python."""

    return isinstance(nodes, dict) and bool(nodes) and all(isinstance(key, str) and key and _reference(value) for key, value in nodes.items())


def _offset(lines: list[bytes], line: int, column: int) -> int:
    """AST columns are UTF-8 byte offsets, including for non-ASCII authored source."""

    return sum(map(len, lines[:line - 1])) + column


def _replace_keywords(text: str, call: ast.Call, replacements: dict[str, str]) -> str:
    """Apply edits backwards so original AST offsets remain valid for every field."""

    data = text.encode()
    lines = data.splitlines(keepends=True)
    edits = []
    present = {item.arg: item for item in call.keywords}
    for key, value in replacements.items():
        if key in present:
            node = present[key].value
            edits.append((_offset(lines, node.lineno, node.col_offset), _offset(lines, node.end_lineno, node.end_col_offset), value.encode()))
        else:
            end = _offset(lines, call.end_lineno, call.end_col_offset) - 1
            prefix = "" if data[:end].rstrip().endswith((b"(", b",")) else ", "
            edits.append((end, end, f"{prefix}{key}={value}".encode()))
    for start, end, value in sorted(edits, reverse=True):
        data = data[:start] + value + data[end:]
    return data.decode()
