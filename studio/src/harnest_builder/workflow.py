"""Static Graph topology editing without importing authored modules."""

from __future__ import annotations

import ast

from fastapi import HTTPException


def graph_call(text: str) -> ast.Call | None:
    """Only edit the exported root graph; nested or dynamic declarations stay in source."""
    tree = ast.parse(text)
    for statement in tree.body:
        if _root_assignment(statement):
            call = statement.value
            if isinstance(call, ast.Call) and ast.unparse(call.func) == "Graph":
                return call
    return None


def _root_assignment(statement: ast.AST) -> bool:
    """Identify the exported root without accidentally editing a nested helper graph."""
    return isinstance(statement, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "root_agent" for t in statement.targets)


def describe(text: str) -> dict:
    """Read only statically authored node keys and literal Edge calls."""
    call = graph_call(text)
    if call is None:
        return {"available": False, "reason": "This root is an Agent or a dynamic graph. Edit its source to configure composition."}
    try:
        return _topology(call)
    except (KeyError, ValueError, TypeError):
        return {"available": False, "reason": "This graph computes its topology in Python. Use the code editor to preserve that logic."}


def _topology(call: ast.Call) -> dict:
    """Retain inline node expressions while exposing only safe names and literal edges."""
    values = {k.arg: k.value for k in call.keywords}
    nodes, edges = values["nodes"], values["edges"]
    if not isinstance(nodes, ast.Dict) or not isinstance(edges, (ast.List, ast.Tuple)):
        raise ValueError("Dynamic graph")
    keys = [ast.literal_eval(k) for k in nodes.keys]
    if not _valid_keys(keys):
        raise ValueError("Dynamic node names")
    return {"available": True, "nodes": [_node(k, v) for k, v in zip(keys, nodes.values)], "edges": [_edge(e) for e in edges.elts]}


def _node(identity: str, value: ast.AST) -> dict:
    """Expose discovered resource references so the inspector opens their actual source."""
    reference = value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else None
    return {"id": identity, "expression": ast.unparse(value), "reference": reference}


def _valid_keys(keys: list) -> bool:
    """Require unique, nonempty node names distinct from the entry sentinel."""
    return all(isinstance(k, str) and k and k != "START" for k in keys) and len(set(keys)) == len(keys)


def _edge(node: ast.AST) -> dict:
    """Reject custom edge constructors and expressions instead of silently losing them."""
    if not isinstance(node, ast.Call) or ast.unparse(node.func) != "Edge":
        raise ValueError("Dynamic edge")
    values = dict(zip(("source", "target", "route"), node.args))
    values.update({k.arg: k.value for k in node.keywords})
    if not {"source", "target"} <= values.keys() or values.keys() - {"source", "target", "route"}:
        raise ValueError("Unsupported edge")
    return {key: "START" if isinstance(value, ast.Name) and value.id == "START" else ast.literal_eval(value) for key, value in values.items()}


def replace_expression(text: str, node: ast.AST, expression: str) -> str:
    """Use UTF-8 AST offsets to preserve all source outside the reviewed expression."""
    lines = text.encode().splitlines(keepends=True)
    start = sum(map(len, lines[:node.lineno - 1])) + node.col_offset
    end = sum(map(len, lines[:node.end_lineno - 1])) + node.end_col_offset
    raw = text.encode()
    return (raw[:start] + expression.encode() + raw[end:]).decode()


def connect(text: str, edges: list[dict]) -> str:
    """Replace explicit topology only after validating every source and destination."""
    topology = describe(text)
    if not topology["available"]:
        raise HTTPException(422, topology["reason"])
    nodes = {n["id"] for n in topology["nodes"]}
    rendered = []
    seen = set()
    for edge in edges:
        _validate_edge(edge, nodes, seen)
        rendered.append(f"Edge({edge['source']!r}, {edge['target']!r}, route={edge.get('route')!r})")
    call = graph_call(text)
    expression = next(k.value for k in call.keywords if k.arg == "edges")
    return replace_expression(text, expression, "[" + ", ".join(rendered) + "]")


def _validate_edge(edge: dict, nodes: set, seen: set) -> None:
    """Reject invalid endpoints and duplicate routing without restricting legitimate graph cycles."""
    if edge.keys() - {"source", "target", "route"}:
        raise HTTPException(422, "Unsupported graph edge fields.")
    if edge.get("source") not in nodes | {"START"} or edge.get("target") not in nodes:
        raise HTTPException(422, "Connect existing nodes; START can only be a source.")
    route = edge.get("route")
    if route is not None and not isinstance(route, str):
        raise HTTPException(422, "A conditional route must be text.")
    key = (edge["source"], edge["target"], route)
    if key in seen:
        raise HTTPException(422, "This connection already exists.")
    seen.add(key)


def add_node(text: str, identity: str) -> str:
    """Wire a discovered resource by name without importing filesystem folders as packages."""
    topology = describe(text)
    if not topology["available"]:
        raise HTTPException(422, "Graph agents require a static root Graph; use Subagent for a managed ADK Agent.")
    if identity in {n["id"] for n in topology["nodes"]}:
        raise HTTPException(409, "This graph node already exists.")
    call = graph_call(text)
    nodes = next(k.value for k in call.keywords if k.arg == "nodes")
    expressions = [f"{ast.literal_eval(k)!r}: {ast.get_source_segment(text, v)}" for k, v in zip(nodes.keys, nodes.values)]
    # The compiler resolves strings from its discovered-resource registry; bare
    # subagents imports do not exist in the isolated authoring import context.
    expressions.append(f"{identity!r}: {identity!r}")
    return replace_expression(text, nodes, "{\n        " + ",\n        ".join(expressions) + "\n    }")


def to_graph(text: str, instructions: str) -> str:
    """Wrap a static root Agent as a Graph node, retaining its authored model and settings."""
    if graph_call(text) is not None:
        raise HTTPException(409, "This project already has a root Graph.")
    statement = next((s for s in ast.parse(text).body if _root_assignment(s)), None)
    call = statement.value if statement is not None else None
    if not isinstance(call, ast.Call) or ast.unparse(call.func) != "Agent":
        raise HTTPException(422, "Only a static Agent root can be converted visually. Edit native or dynamic roots in source.")
    expression = ast.get_source_segment(text, call)
    if not any(k.arg == "instruction" for k in call.keywords):
        # A node needs explicit instructions; preserve the former root's file-backed prompt.
        expression = expression[:-1].rstrip().rstrip(",") + f",\n    instruction={instructions!r},\n)"
    identity = _keyword_source(text, call, "name", "'workflow'")
    replacement = "Graph(\n    name=" + identity + ",\n    nodes={'respond': " + expression + "},\n    edges=[Edge(START, 'respond')],\n)"
    result = replace_expression(text, call, replacement)
    lines = result.splitlines(keepends=True)
    lines.insert(statement.lineno - 1, "from harnest.graph import Graph, Edge, START\n\n")
    return "".join(lines)


def _keyword_source(text: str, call: ast.Call, key: str, default: str) -> str:
    """Copy a constructor setting without evaluating authored Python expressions."""
    return next((ast.get_source_segment(text, k.value) for k in call.keywords if k.arg == key), default)
