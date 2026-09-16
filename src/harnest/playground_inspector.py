"""Build a non-executing Studio projection from a Harnest workspace."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Iterable, Mapping

import yaml

from .source_tree import ignored_source_path


_CONFIG_LIMIT = 1024 * 1024
_MANIFEST_LIMIT = 1024 * 1024
_SKILL_FRONTMATTER_LIMIT = 16 * 1024
_STUDIO_IGNORED_DIRECTORIES = frozenset({"cache"})
_DECLARATION_CALLS = {
    "Agent": "agent",
    "AgentDefinition": "agent",
    "Cron": "schedule",
    "Graph": "graph",
    "Join": "join",
    "LiteLLMModel": "model",
    "MCPClient": "mcp",
    "Sandbox": "sandbox",
}
_DECORATOR_KINDS = {
    "provider": "context",
    "lifecycle": "lifecycle",
    "task": "task",
    "tool": "tool",
    "client_tool": "tool",
}
_LIFECYCLE_ROLES = frozenset(
    {
        "adk_plugin",
        "agent.after",
        "agent.before",
        "agent.on_event",
        "agent.on_error",
        "authenticate",
        "credential_provider",
        "http.after",
        "http.before",
        "http.on_error",
        "http_routes",
        "langgraph_middleware",
        "mcp.after",
        "mcp.before",
        "mcp.on_error",
        "model.after",
        "model.before",
        "model.on_error",
        "output_policy",
        "resource",
        "skills.source",
        "storage.assets",
        "storage.checkpoints",
        "storage.custom",
        "storage.sessions",
        "telemetry_exporter",
        "tool.after",
        "tool.before",
        "tool.on_error",
    }
)
_SAFE_KEYWORDS = {
    "agent": frozenset(
        {
            "description",
            "history",
            "input_schema",
            "max_concurrency",
            "mcp",
            "model",
            "name",
            "output_key",
            "output_schema",
            "sandboxes",
            "subagents",
            "tools",
        }
    ),
    "graph": frozenset({"description", "max_concurrency"}),
    "mcp": frozenset(
        {"prefix", "read_timeout_seconds", "timeout_seconds", "tools", "transport"}
    ),
    "model": frozenset({"api_base", "model", "provider", "temperature"}),
    "sandbox": frozenset(
        {"image", "max_output_bytes", "max_scopes", "name", "scope", "timeout_seconds"}
    ),
    "schedule": frozenset({"schedule", "task", "timezone"}),
}


class StudioInspectionError(ValueError):
    """A folder cannot be treated as a Harnest Studio workspace."""


@dataclass(frozen=True, slots=True)
class SourceFile:
    """Content identity and filesystem metadata without canonical source text."""

    path: str
    digest: str
    size: int
    mtime_ns: int
    language: str

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-safe file metadata persisted by Studio."""

        return {
            "path": self.path,
            "digest": self.digest,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "language": self.language,
        }


@dataclass(frozen=True, slots=True)
class StudioBlock:
    """A statically recognized Harnest declaration or graph node."""

    id: str
    kind: str
    name: str
    path: str
    line: int
    end_line: int
    config: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a transport-safe declaration record."""

        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "path": self.path,
            "line": self.line,
            "end_line": self.end_line,
            "config": dict(self.config),
        }


@dataclass(frozen=True, slots=True)
class StudioConnection:
    """A directed edge resolved to stable block identifiers."""

    graph: str
    source: str
    target: str
    route: Any | None = None
    path: str = ""
    line: int = 0
    kind: str = "workflow"

    def to_dict(self) -> dict[str, Any]:
        """Return a transport-safe graph edge."""

        value = {
            "kind": self.kind,
            "graph": self.graph,
            "source": self.source,
            "target": self.target,
            "path": self.path,
            "line": self.line,
        }
        if self.route is not None:
            value["route"] = self.route
        return value


@dataclass(frozen=True, slots=True)
class StudioDiagnostic:
    """A static inspection problem that did not require importing user code."""

    severity: str
    code: str
    message: str
    path: str = ""
    line: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return a transport-safe diagnostic."""

        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "line": self.line,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceProjection:
    """The derived, reproducible Studio view of a canonical source folder."""

    workspace_path: str
    source_digest: str
    files: tuple[SourceFile, ...]
    blocks: tuple[StudioBlock, ...]
    connections: tuple[StudioConnection, ...]
    diagnostics: tuple[StudioDiagnostic, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the complete JSON-safe projection without source contents."""

        return {
            "workspace_path": self.workspace_path,
            "source_digest": self.source_digest,
            "files": [item.to_dict() for item in self.files],
            "blocks": [item.to_dict() for item in self.blocks],
            "connections": [item.to_dict() for item in self.connections],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


@dataclass(slots=True)
class _Inspection:
    """Mutable assembly state kept private from the immutable result contract."""

    files: list[SourceFile] = field(default_factory=list)
    blocks: list[StudioBlock] = field(default_factory=list)
    connections: list[StudioConnection] = field(default_factory=list)
    diagnostics: list[StudioDiagnostic] = field(default_factory=list)


def inspect_workspace(
    path: str | Path, *, require_config: bool = True, mode: str | None = None
) -> WorkspaceProjection:
    """Inspect declarations and graph structure without importing authored code."""

    root = _workspace_root(path, require_config=require_config)
    state = _Inspection()
    for relative, absolute in _source_files(root, state.diagnostics):
        metadata = _file_metadata(relative, absolute)
        state.files.append(metadata)
        if relative.suffix == ".py":
            _inspect_python(relative, absolute, state)
        elif _is_agent_plugin_manifest(relative):
            _inspect_agent_plugin(relative, absolute, state)
        elif _is_extension_manifest(relative):
            _inspect_extension(relative, absolute, state)
        elif _is_skill_file(relative):
            _inspect_skill(relative, absolute, state)
    _inspect_composition(root, state, mode)
    files = tuple(sorted(state.files, key=lambda item: item.path))
    return WorkspaceProjection(
        workspace_path=str(root),
        source_digest=_source_digest(files),
        files=files,
        blocks=tuple(sorted(state.blocks, key=_block_order)),
        connections=tuple(sorted(state.connections, key=_connection_order)),
        diagnostics=tuple(sorted(state.diagnostics, key=_diagnostic_order)),
    )


def _inspect_composition(root: Path, state: _Inspection, mode: str | None) -> None:
    """Infer folder ownership only under the effective managed compiler mode."""

    if (root / "config.yaml").is_file():
        _inspect_config(root / "config.yaml", state)
    # Advanced frameworks own their wiring; folder proximity cannot establish it.
    workspace = next((b for b in state.blocks if b.kind == "workspace"), None)
    effective_mode = mode or (workspace.config.get("framework_mode") if workspace else None)
    if effective_mode == "managed":
        _inspect_agent_composition(state)


def _workspace_root(path: str | Path, *, require_config: bool) -> Path:
    """Resolve one real folder and require its regular configuration marker."""

    candidate = Path(path).expanduser().resolve()
    if not candidate.is_dir():
        raise StudioInspectionError(f"Studio workspace is not a directory: {candidate}")
    config = candidate / "config.yaml"
    try:
        info = config.lstat()
    except FileNotFoundError as exc:
        if not require_config:
            return candidate
        raise StudioInspectionError(
            f"Harnest Studio workspace requires config.yaml: {candidate}"
        ) from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size > _CONFIG_LIMIT:
        raise StudioInspectionError("config.yaml must be a regular file of at most 1 MiB")
    return candidate


def _source_files(
    root: Path, diagnostics: list[StudioDiagnostic]
) -> Iterable[tuple[Path, Path]]:
    """Walk the shared source identity while refusing to follow authored links."""

    for directory, names, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        relative_directory = current.relative_to(root)
        names[:] = _visible_directories(current, relative_directory, names, diagnostics)
        for name in sorted(filenames):
            relative = relative_directory / name
            absolute = current / name
            if ignored_source_path(relative):
                continue
            if absolute.is_symlink():
                diagnostics.append(
                    StudioDiagnostic(
                        "warning",
                        "symlink_ignored",
                        "Studio does not inspect symlinked files",
                        str(relative),
                    )
                )
                continue
            if absolute.is_file():
                yield relative, absolute


def _visible_directories(
    current: Path,
    relative: Path,
    names: list[str],
    diagnostics: list[StudioDiagnostic],
) -> list[str]:
    """Prune ignored and linked directories before the filesystem walker enters them."""

    visible: list[str] = []
    for name in sorted(names):
        child_relative = relative / name
        child = current / name
        if ignored_source_path(child_relative) or name in _STUDIO_IGNORED_DIRECTORIES:
            continue
        if child.is_symlink():
            diagnostics.append(
                StudioDiagnostic(
                    "warning",
                    "symlink_ignored",
                    "Studio does not inspect symlinked directories",
                    str(child_relative),
                )
            )
            continue
        visible.append(name)
    return visible


def _file_metadata(relative: Path, absolute: Path) -> SourceFile:
    """Hash a regular file in chunks without retaining its canonical contents."""

    digest = hashlib.sha256()
    with absolute.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    info = absolute.stat()
    return SourceFile(
        path=relative.as_posix(),
        digest=digest.hexdigest(),
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        language=_language(relative),
    )


def _language(path: Path) -> str:
    """Classify common authored formats without inspecting their contents."""

    return {
        ".json": "json",
        ".md": "markdown",
        ".mdx": "markdown",
        ".py": "python",
        ".toml": "toml",
        ".yaml": "yaml",
        ".yml": "yaml",
    }.get(path.suffix.lower(), "text")


def _source_digest(files: tuple[SourceFile, ...]) -> str:
    """Combine path and content identities so mtimes never cause false changes."""

    digest = hashlib.sha256()
    for item in files:
        digest.update(item.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.digest.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _inspect_python(relative: Path, absolute: Path, state: _Inspection) -> None:
    """Parse only module-level declarations and leave all business bodies opaque."""

    try:
        source = absolute.read_text(encoding="utf-8")
        module = ast.parse(source, filename=str(relative))
    except (OSError, UnicodeError, SyntaxError) as exc:
        state.diagnostics.append(_python_diagnostic(relative, exc))
        return
    symbols: dict[str, StudioBlock] = {}
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    graphs: list[tuple[str, ast.Call, StudioBlock]] = []
    for statement in module.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[statement.name] = statement
            mcp_block = _mcp_client_block(relative, statement)
            if mcp_block is not None:
                state.blocks.append(mcp_block)
                symbols[mcp_block.name] = mcp_block
                continue
        block = _function_block(relative, statement)
        if block is not None:
            state.blocks.append(block)
            symbols[block.name] = block
            continue
        assigned = _assigned_call(statement)
        if assigned is None:
            continue
        name, call = assigned
        kind = _call_kind(call, relative)
        if kind is None:
            continue
        block = _call_block(relative, name, kind, call)
        state.blocks.append(block)
        symbols[name] = block
        if kind == "graph":
            graphs.append((name, call, block))
    for _, call, graph in graphs:
        _inspect_graph(relative, call, graph, symbols, functions, state)


def _python_diagnostic(relative: Path, exc: Exception) -> StudioDiagnostic:
    """Normalize parser and decoding failures without exposing file contents."""

    line = getattr(exc, "lineno", 0) or 0
    if isinstance(exc, SyntaxError):
        message = f"SyntaxError: {exc.msg}"
    elif isinstance(exc, UnicodeError):
        message = "Python source must use UTF-8 encoding"
    else:
        message = f"Unable to read Python source: {type(exc).__name__}"
    return StudioDiagnostic(
        "error", "python_parse_error", message, str(relative), line
    )


def _function_block(relative: Path, statement: ast.stmt) -> StudioBlock | None:
    """Recognize decorated functions from headers while deliberately skipping bodies."""

    if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    decorators = [_decorator_name(item) for item in statement.decorator_list]
    roles = tuple(role for name in decorators if (role := _lifecycle_role(name)))
    kind = "lifecycle" if roles else _decorator_kind(decorators)
    if kind is None:
        return None
    config = _function_config(statement, kind, roles)
    return StudioBlock(
        id=_block_id(relative, kind, statement.name),
        kind=kind,
        name=statement.name,
        path=relative.as_posix(),
        line=statement.lineno,
        end_line=statement.end_lineno or statement.lineno,
        config=config,
    )


def _function_config(
    statement: ast.FunctionDef | ast.AsyncFunctionDef,
    kind: str,
    roles: tuple[str, ...],
) -> dict[str, Any]:
    """Project a declaration boundary and only its explicitly safe decorator metadata."""

    config: dict[str, Any] = {
        "async": isinstance(statement, ast.AsyncFunctionDef),
        "parameters": [_parameter(item) for item in statement.args.args],
    }
    if roles:
        config["role"] = roles[0]
        if len(roles) > 1:
            config["roles"] = list(roles)
    config.update(_decorator_config(statement, kind))
    context_name = _context_name(statement.decorator_list)
    if context_name is not None:
        config["name" if kind == "context" else "context"] = context_name
    return config


def _decorator_call(node: ast.expr) -> ast.Call:
    """Normalize bare and configured decorators into one static call shape."""

    return node if isinstance(node, ast.Call) else ast.Call(func=node, args=[], keywords=[])


def _decorator_name(node: ast.expr) -> str:
    """Return the dotted callable selected by a bare or configured decorator."""

    return _qualified_name(_decorator_call(node).func)


def _lifecycle_role(name: str) -> str | None:
    """Accept only supported roles selected through the lifecycle namespace."""

    prefix = "lifecycle."
    if not name.startswith(prefix):
        return None
    role = name[len(prefix) :]
    return role if role in _LIFECYCLE_ROLES else None


def _decorator_kind(names: list[str]) -> str | None:
    """Map non-lifecycle Harnest decorators while preserving configured context."""

    for name in names:
        tail = name.rsplit(".", 1)[-1]
        if tail in _DECORATOR_KINDS:
            return _DECORATOR_KINDS[tail]
    return None


def _decorator_config(
    statement: ast.FunctionDef | ast.AsyncFunctionDef, kind: str
) -> dict[str, Any]:
    """Project only non-secret Harnest decorator options."""

    allowed = {
        "task": frozenset({"max_retries", "queue"}),
        "tool": frozenset({"description", "durable", "permission"}),
    }.get(kind, frozenset())
    result: dict[str, Any] = {}
    if kind == "lifecycle":
        return result
    for decorator in statement.decorator_list:
        call = _decorator_call(decorator)
        if _qualified_name(call.func).rsplit(".", 1)[-1] != kind:
            continue
        result.update(_safe_keywords(call, allowed))
    return result


def _context_name(decorators: list[ast.expr]) -> str | None:
    """Read only the literal resource name from ``@context.provider``."""

    for decorator in decorators:
        call = _decorator_call(decorator)
        if _qualified_name(call.func).rsplit(".", 1)[-1] != "provider":
            continue
        if call.args:
            return _string_literal(call.args[0])
    return None


def _mcp_client_block(
    relative: Path, function: ast.FunctionDef | ast.AsyncFunctionDef
) -> StudioBlock | None:
    """Recognize the conventional zero-argument MCP factory's direct return only."""

    if not _is_mcp_module(relative) or function.name != "client":
        return None
    if _function_has_arguments(function):
        return None
    returned = _direct_return(function)
    if not isinstance(returned, ast.Call):
        return None
    qualified = _qualified_name(returned.func)
    transport = qualified.rsplit(".", 1)[-1]
    if qualified.rsplit(".", 1)[0] != "MCPClient" or transport not in {
        "sse",
        "stdio",
        "streamable_http",
    }:
        return None
    name = relative.stem
    config = _safe_keywords(returned, _SAFE_KEYWORDS["mcp"])
    config["transport"] = transport
    return StudioBlock(
        id=_block_id(relative, "mcp", name),
        kind="mcp",
        name=name,
        path=relative.as_posix(),
        line=function.lineno,
        end_line=function.end_lineno or function.lineno,
        config=config,
    )


def _is_mcp_module(relative: Path) -> bool:
    """Limit MCP factory body inspection to the documented mcp/<name>.py convention."""

    return len(relative.parts) == 2 and relative.parts[0] == "mcp" and relative.suffix == ".py"


def _function_has_arguments(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Reject factories whose invocation would require or accept authored input."""

    arguments = function.args
    return bool(
        arguments.posonlyargs
        or arguments.args
        or arguments.kwonlyargs
        or arguments.vararg
        or arguments.kwarg
    )


def _direct_return(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> ast.expr | None:
    """Allow a docstring plus one return without traversing any other function body."""

    statements = list(function.body)
    if statements and _is_docstring(statements[0]):
        statements.pop(0)
    if len(statements) == 1 and isinstance(statements[0], ast.Return):
        return statements[0].value
    return None


def _is_docstring(statement: ast.stmt) -> bool:
    """Recognize a function docstring without retaining its text."""

    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


def _parameter(argument: ast.arg) -> dict[str, str]:
    """Describe a callable boundary without traversing or retaining its implementation."""

    value = {"name": argument.arg}
    if argument.annotation is not None:
        value["annotation"] = ast.unparse(argument.annotation)
    return value


def _assigned_call(statement: ast.stmt) -> tuple[str, ast.Call] | None:
    """Return a simple module assignment whose value is a constructor call."""

    target: ast.expr | None = None
    value: ast.expr | None = None
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        target, value = statement.targets[0], statement.value
    elif isinstance(statement, ast.AnnAssign):
        target, value = statement.target, statement.value
    if isinstance(target, ast.Name) and isinstance(value, ast.Call):
        return target.id, value
    return None


def _call_kind(call: ast.Call, relative: Path | None = None) -> str | None:
    """Map supported constructor spellings to Studio block kinds."""

    qualified = _qualified_name(call.func)
    tail = qualified.rsplit(".", 1)[-1]
    if tail in {"stdio", "sse", "streamable_http"} and "MCPClient" in qualified:
        return "mcp"
    if tail in {"provider", "docker", "subprocess"} and "Sandbox" in qualified:
        return "sandbox"
    if tail == "sandbox" and relative is not None and _resource_scope(relative, "sandbox") is not None:
        # Extension-owned constructors such as docker.sandbox() still satisfy
        # the canonical sandbox/<name>.py export contract.
        return "sandbox"
    if qualified.endswith("LiteLLMModel.from_openai_environment"):
        return "model"
    if qualified.endswith("Agent.advanced"):
        return "agent"
    return _DECLARATION_CALLS.get(tail)


def _qualified_name(node: ast.expr) -> str:
    """Render a static dotted reference without resolving or importing it."""

    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _qualified_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _call_block(relative: Path, name: str, kind: str, call: ast.Call) -> StudioBlock:
    """Create one block from an assigned Harnest constructor."""

    config = _safe_keywords(call, _SAFE_KEYWORDS.get(kind, frozenset()))
    if kind == "agent":
        _project_agent_identity(call, config)
    if kind == "model" and "model" not in config and call.args:
        positional_model = _safe_value(call.args[0])
        # LiteLLMModel's public model identifier is positional in normal Harnest code.
        if isinstance(positional_model, str):
            config["model"] = positional_model
    config["constructor"] = _qualified_name(call.func)
    return StudioBlock(
        id=_block_id(relative, kind, name),
        kind=kind,
        name=name,
        path=relative.as_posix(),
        line=call.lineno,
        end_line=call.end_lineno or call.lineno,
        config=config,
    )


def _project_agent_identity(call: ast.Call, config: dict[str, Any]) -> None:
    """Expose public agent identity while keeping dynamic model setup opaque."""

    for position, name in ((0, "name"), (1, "model")):
        if name in config:
            continue
        node = _argument(call, position, name)
        value = _safe_value(node) if node is not None else _UNAVAILABLE
        if value is _UNAVAILABLE and name == "model" and isinstance(node, ast.Call):
            constructor = _qualified_name(node.func)
            value = {"constructor": constructor} if constructor else _UNAVAILABLE
        if value is not _UNAVAILABLE:
            config[name] = value


def _safe_keywords(call: ast.Call, allowed: frozenset[str]) -> dict[str, Any]:
    """Keep allow-listed literal metadata and discard dynamic or secret-bearing expressions."""

    result: dict[str, Any] = {}
    for keyword in call.keywords:
        if keyword.arg not in allowed:
            continue
        value = _safe_value(keyword.value)
        if value is not _UNAVAILABLE:
            result[keyword.arg] = value
    return result


_UNAVAILABLE = object()


def _safe_value(node: ast.expr) -> Any:
    """Decode bounded literals and references without evaluating authored code."""

    scalar_types = (str, int, float, bool, type(None))
    if isinstance(node, ast.Constant) and isinstance(node.value, scalar_types):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)) and len(node.elts) <= 100:
        values = [_safe_value(item) for item in node.elts]
        return values if all(item is not _UNAVAILABLE for item in values) else _UNAVAILABLE
    if isinstance(node, (ast.Name, ast.Attribute)):
        return {"reference": _qualified_name(node)}
    return _UNAVAILABLE


def _inspect_graph(
    relative: Path,
    call: ast.Call,
    graph: StudioBlock,
    symbols: dict[str, StudioBlock],
    functions: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef],
    state: _Inspection,
) -> None:
    """Resolve graph nodes and edges from literal constructor arguments only."""

    nodes = _keyword(call, "nodes")
    edges = _keyword(call, "edges")
    node_ids = _graph_nodes(relative, nodes, graph, symbols, functions, state)
    node_ids["START"] = f"{graph.id}/START"
    # Retain container ownership even for nodes with no connected workflow edges.
    graph.config["nodes"] = dict(node_ids)
    state.blocks.append(
        StudioBlock(
            node_ids["START"],
            "input",
            "START",
            relative.as_posix(),
            graph.line,
            graph.line,
        )
    )
    for edge in _sequence(edges):
        connection = _graph_edge(relative, edge, graph, node_ids, state.diagnostics)
        if connection is not None:
            state.connections.append(connection)


def _graph_nodes(
    relative: Path,
    node: ast.expr | None,
    graph: StudioBlock,
    symbols: dict[str, StudioBlock],
    functions: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef],
    state: _Inspection,
) -> dict[str, str]:
    """Map graph references to existing symbols or statically nested declarations."""

    result: dict[str, str] = {}
    if not isinstance(node, ast.Dict):
        return result
    for key_node, value in zip(node.keys, node.values):
        reference = _string_literal(key_node)
        if reference is None:
            continue
        if isinstance(value, ast.Name) and value.id in symbols:
            result[reference] = symbols[value.id].id
            continue
        if isinstance(value, ast.Name) and value.id in functions:
            block = _opaque_function_block(relative, functions[value.id])
            symbols[value.id] = block
            state.blocks.append(block)
            result[reference] = block.id
            continue
        nested = _nested_graph_block(relative, reference, value, graph)
        if nested is not None:
            state.blocks.append(nested)
            result[reference] = nested.id
    return result


def _opaque_function_block(
    relative: Path, function: ast.FunctionDef | ast.AsyncFunctionDef
) -> StudioBlock:
    """Expose a referenced graph callable's boundary while keeping its body opaque."""

    config = {
        "async": isinstance(function, ast.AsyncFunctionDef),
        "parameters": [_parameter(item) for item in function.args.args],
    }
    return StudioBlock(
        id=_block_id(relative, "function", function.name),
        kind="function",
        name=function.name,
        path=relative.as_posix(),
        line=function.lineno,
        end_line=function.end_lineno or function.lineno,
        config=config,
    )


def _nested_graph_block(
    relative: Path, reference: str, node: ast.expr, graph: StudioBlock
) -> StudioBlock | None:
    """Represent an inline graph declaration without descending into callable bodies."""

    if not isinstance(node, ast.Call):
        return None
    kind = _call_kind(node) or "function"
    return StudioBlock(
        id=f"{graph.id}/nodes/{reference}",
        kind=kind,
        name=reference,
        path=relative.as_posix(),
        line=node.lineno,
        end_line=node.end_lineno or node.lineno,
        config=_safe_keywords(node, _SAFE_KEYWORDS.get(kind, frozenset())),
    )


def _graph_edge(
    relative: Path,
    node: ast.expr,
    graph: StudioBlock,
    node_ids: Mapping[str, str],
    diagnostics: list[StudioDiagnostic],
) -> StudioConnection | None:
    """Decode one literal Edge call and diagnose unresolved graph references."""

    if not isinstance(node, ast.Call) or _qualified_name(node.func).rsplit(".", 1)[-1] != "Edge":
        return None
    source = _edge_argument(node, 0, "source")
    target = _edge_argument(node, 1, "target")
    if source is None or target is None:
        diagnostics.append(
            StudioDiagnostic(
                "warning",
                "dynamic_edge",
                "Studio cannot resolve a dynamic graph edge",
                relative.as_posix(),
                node.lineno,
            )
        )
        return None
    if source not in node_ids or target not in node_ids:
        diagnostics.append(
            StudioDiagnostic(
                "error",
                "unknown_graph_node",
                f"Graph edge references an unknown node: {source} -> {target}",
                relative.as_posix(),
                node.lineno,
            )
        )
        return None
    route_node = _argument(node, 2, "route")
    route = _safe_value(route_node) if route_node is not None else None
    return StudioConnection(
        graph=graph.id,
        source=node_ids[source],
        target=node_ids[target],
        route=None if route is _UNAVAILABLE else route,
        path=relative.as_posix(),
        line=node.lineno,
    )


def _edge_argument(call: ast.Call, position: int, name: str) -> str | None:
    """Read a literal edge endpoint, including the public START sentinel."""

    node = _argument(call, position, name)
    if isinstance(node, ast.Name) and node.id == "START":
        return "START"
    return _string_literal(node)


def _argument(call: ast.Call, position: int, name: str) -> ast.expr | None:
    """Select one positional-or-keyword constructor argument without evaluation."""

    if len(call.args) > position:
        return call.args[position]
    return _keyword(call, name)


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    """Select one explicit keyword while ignoring dynamic **mapping expansion."""

    return next((item.value for item in call.keywords if item.arg == name), None)


def _sequence(node: ast.expr | None) -> tuple[ast.expr, ...]:
    """Return only statically enumerable list or tuple elements."""

    return tuple(node.elts) if isinstance(node, (ast.List, ast.Tuple)) else ()


def _string_literal(node: ast.expr | None) -> str | None:
    """Read a string constant without invoking literal or authored helpers."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _inspect_agent_composition(state: _Inspection) -> None:
    """Project filesystem-owned capabilities onto their managed agent scopes."""

    owners = _composition_agents(state.blocks)
    for scope, agents in owners.items():
        targets = _scope_resources(state.blocks, scope)
        for agent in agents:
            for target in targets:
                if _agent_consumes_resource(agent, target):
                    _append_composition_connection(state, agent, target)
            model = _referenced_model(agent, state.blocks)
            if model is not None:
                _append_composition_connection(state, agent, model)
    _connect_nested_agents(state, owners)
    _connect_flat_subagents(state, owners)


def _composition_agents(blocks: list[StudioBlock]) -> dict[str, list[StudioBlock]]:
    """Group conventional agent.py declarations by filesystem ownership scope."""

    result: dict[str, list[StudioBlock]] = {}
    for block in blocks:
        if block.kind != "agent" or not _active_resource_path(Path(block.path)):
            continue
        path = Path(block.path)
        if path.name != "agent.py" or not _is_agent_scope(path.parent.parts):
            continue
        scope = "" if path.parent == Path(".") else path.parent.as_posix()
        result.setdefault(scope, []).append(block)
    return result


def _scope_resources(blocks: list[StudioBlock], scope: str) -> list[StudioBlock]:
    """Return resources that the compiler discovers for one managed agent folder."""

    directory_by_kind = {
        "mcp": "mcp",
        "sandbox": "sandbox",
        "skill": "skills",
        "tool": "tools",
    }
    return [
        block
        for block in blocks
        if (directory := directory_by_kind.get(block.kind)) is not None
        and _resource_scope(Path(block.path), directory) == scope
        and _active_resource_path(Path(block.path))
    ]


def _active_resource_path(relative: Path) -> bool:
    """Mirror compiler inactive-entry naming when inferring resource ownership."""

    return not any(part.startswith(("_", ".")) for part in relative.parts)


def _resource_scope(relative: Path, directory: str) -> str | None:
    """Resolve a conventional resource directory to its owning agent scope."""

    parts = relative.parts
    for index, part in enumerate(parts):
        if part != directory or not _is_agent_scope(parts[:index]):
            continue
        return "/".join(parts[:index])
    return None


def _is_agent_scope(parts: tuple[str, ...]) -> bool:
    """Accept root or recursive subagents/<name> ownership directories only."""

    return len(parts) % 2 == 0 and all(
        parts[index] == "subagents" and bool(parts[index + 1])
        for index in range(0, len(parts), 2)
    )


def _agent_consumes_resource(agent: StudioBlock, resource: StudioBlock) -> bool:
    """Apply the compiler's explicit-grant rule for sandbox resources."""

    if resource.kind != "sandbox":
        return True
    grants = agent.config.get("sandboxes", [])
    return isinstance(grants, list) and resource.name in grants


def _referenced_model(
    agent: StudioBlock, blocks: list[StudioBlock]
) -> StudioBlock | None:
    """Resolve a statically named reusable model selected by an agent."""

    model = agent.config.get("model")
    reference = model.get("reference") if isinstance(model, dict) else None
    if not isinstance(reference, str):
        return None
    name = reference.rsplit(".", 1)[-1]
    candidates = [block for block in blocks if block.kind == "model" and block.name == name]
    local = [block for block in candidates if block.path == agent.path]
    candidates = local or candidates
    # A name alone cannot disambiguate two imported model declarations.
    return candidates[0] if len(candidates) == 1 else None


def _connect_nested_agents(
    state: _Inspection, owners: dict[str, list[StudioBlock]]
) -> None:
    """Connect directory-owned subagents to the agent in their parent scope."""

    for scope, children in owners.items():
        if not scope:
            continue
        parent_scope = "/".join(Path(scope).parts[:-2])
        for parent in owners.get(parent_scope, []):
            for child in children:
                _append_composition_connection(state, parent, child)


def _connect_flat_subagents(
    state: _Inspection, owners: dict[str, list[StudioBlock]]
) -> None:
    """Connect flat subagents/<name>.py declarations to their owning folder."""

    owned_ids = {agent.id for agents in owners.values() for agent in agents}
    for child in state.blocks:
        parent_scope = _flat_subagent_parent_scope(child, owned_ids)
        if parent_scope is None:
            continue
        for parent in owners.get(parent_scope, []):
            _append_composition_connection(state, parent, child)


def _flat_subagent_parent_scope(
    block: StudioBlock, owned_ids: set[str]
) -> str | None:
    """Resolve only filename-based subagent declarations outside agent.py scopes."""

    path = Path(block.path)
    if block.kind != "agent" or block.id in owned_ids or not _active_resource_path(path):
        return None
    parts = path.parts
    if len(parts) < 2 or parts[-2] != "subagents":
        return None
    parent = parts[:-2]
    return "/".join(parent) if _is_agent_scope(parent) else None


def _append_composition_connection(
    state: _Inspection, agent: StudioBlock, target: StudioBlock
) -> None:
    """Add one deterministic ownership edge without duplicating graph topology."""

    if str(agent.config.get("constructor", "")).endswith("Agent.advanced"):
        return
    if any(
        edge.source == agent.id and edge.target == target.id
        for edge in state.connections
    ):
        return
    state.connections.append(
        StudioConnection(
            graph=agent.id,
            source=agent.id,
            target=target.id,
            path=target.path,
            line=target.line,
            kind="delegation" if target.kind == "agent" else "capability",
        )
    )


def _is_skill_file(relative: Path) -> bool:
    """Recognize root, Agent Plugin, and Harnest Extension skill entrypoints."""

    root_skill = (
        len(relative.parts) >= 3 and relative.parts[-3] == "skills"
        and _is_agent_scope(relative.parts[:-3])
    )
    package_skill = (
        len(relative.parts) == 5
        and relative.parts[0] in {"plugins", "extensions"}
        and relative.parts[2] == "skills"
    )
    return relative.name == "SKILL.md" and (root_skill or package_skill)


def _is_agent_plugin_manifest(relative: Path) -> bool:
    """Match only the canonical portable plugin descriptor location."""

    return (
        len(relative.parts) == 3
        and relative.parts[0] == "plugins"
        and relative.name == "plugin.json"
    )


def _is_extension_manifest(relative: Path) -> bool:
    """Match only the canonical Harnest Extension descriptor location."""

    return (
        len(relative.parts) == 3
        and relative.parts[0] == "extensions"
        and relative.name == "extension.yaml"
    )


def _inspect_agent_plugin(relative: Path, path: Path, state: _Inspection) -> None:
    """Project public Agent Plugins 1.0 identity without reading client secrets."""

    value = _read_json_manifest(relative, path, state.diagnostics)
    if value is None:
        return
    name = value.get("name")
    if not isinstance(name, str):
        name = relative.parent.name
    config: dict[str, Any] = _string_fields(
        value, ("name", "version", "description")
    )
    config["standard"] = "Agent Plugins 1.0"
    config["mcp"] = (path.parent / "mcp.json").is_file()
    config["skills"] = (path.parent / "skills").is_dir()
    state.blocks.append(
        StudioBlock(
            id=_block_id(relative, "agent_plugin", name),
            kind="agent_plugin",
            name=name,
            path=relative.as_posix(),
            line=1,
            end_line=1,
            config=config,
        )
    )


def _read_json_manifest(
    relative: Path,
    path: Path,
    diagnostics: list[StudioDiagnostic],
) -> Mapping[str, Any] | None:
    """Decode one bounded JSON object and reject ambiguous duplicate keys."""

    try:
        source = _bounded_manifest_text(path)
        value = json.loads(
            source,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        diagnostics.append(_manifest_diagnostic(relative, "plugin", exc))
        return None
    if not isinstance(value, dict):
        diagnostics.append(_manifest_diagnostic(relative, "plugin", TypeError()))
        return None
    return value


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate manifest fields instead of silently selecting one value."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    """Reject non-standard numeric constants accepted by Python's JSON decoder."""

    raise ValueError("non-finite JSON number")


def _inspect_extension(relative: Path, path: Path, state: _Inspection) -> None:
    """Project a Harnest Extension's declared identity and authority only."""

    value = _read_yaml_manifest(relative, path, state.diagnostics)
    if value is None:
        return
    metadata = (
        value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
    )
    requires = (
        value.get("requires") if isinstance(value.get("requires"), dict) else {}
    )
    name = metadata.get("name")
    if not isinstance(name, str):
        name = relative.parent.name
    config: dict[str, Any] = _string_fields(metadata, ("name", "version"))
    capabilities = _string_sequence(value.get("capabilities"))
    dependencies = _string_sequence(requires.get("extensions"))
    if capabilities:
        config["capabilities"] = capabilities
    if dependencies:
        config["requires"] = dependencies
    config["entrypoint"] = "extension:extension"
    state.blocks.append(
        StudioBlock(
            id=_block_id(relative, "extension", name),
            kind="extension",
            name=name,
            path=relative.as_posix(),
            line=1,
            end_line=1,
            config=config,
        )
    )


def _read_yaml_manifest(
    relative: Path,
    path: Path,
    diagnostics: list[StudioDiagnostic],
) -> Mapping[str, Any] | None:
    """Decode one bounded safe YAML mapping without retaining source values."""

    try:
        documents = list(yaml.safe_load_all(_bounded_manifest_text(path)))
    except (OSError, UnicodeError, ValueError, RecursionError, yaml.YAMLError) as exc:
        diagnostics.append(_manifest_diagnostic(relative, "extension", exc))
        return None
    if len(documents) != 1 or not isinstance(documents[0], dict):
        diagnostics.append(_manifest_diagnostic(relative, "extension", TypeError()))
        return None
    return documents[0]


def _bounded_manifest_text(path: Path) -> str:
    """Read manifest text under the compiler's one-megabyte descriptor limit."""

    with path.open("rb") as stream:
        source = stream.read(_MANIFEST_LIMIT + 1)
    if len(source) > _MANIFEST_LIMIT:
        raise ValueError("manifest exceeds limit")
    return source.decode("utf-8")


def _manifest_diagnostic(
    relative: Path, kind: str, exc: Exception
) -> StudioDiagnostic:
    """Report a safe descriptor failure without copying parser or source text."""

    return StudioDiagnostic(
        "warning",
        f"{kind}_manifest_error",
        f"Unable to project {kind} manifest: {type(exc).__name__}",
        relative.as_posix(),
    )


def _string_sequence(value: Any) -> list[str]:
    """Copy a manifest sequence only when every member is public string metadata."""

    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return []
    return list(value)


def _inspect_skill(relative: Path, path: Path, state: _Inspection) -> None:
    """Project bounded public skill frontmatter without retaining instructional content."""

    config = _skill_frontmatter(relative, path, state.diagnostics)
    name = str(config.get("name", relative.parent.name))
    state.blocks.append(
        StudioBlock(
            id=_block_id(relative, "skill", name),
            kind="skill",
            name=name,
            path=relative.as_posix(),
            line=1,
            end_line=1,
            config=config,
        )
    )


def _skill_frontmatter(
    relative: Path, path: Path, diagnostics: list[StudioDiagnostic]
) -> dict[str, str]:
    """Decode a small YAML header and ignore the remainder of the skill document."""

    try:
        with path.open("rb") as stream:
            prefix = stream.read(_SKILL_FRONTMATTER_LIMIT + 1)
        text = prefix[:_SKILL_FRONTMATTER_LIMIT].decode("utf-8")
    except (OSError, UnicodeError) as exc:
        diagnostics.append(
            StudioDiagnostic(
                "error",
                "skill_frontmatter_error",
                f"Unable to read skill frontmatter: {type(exc).__name__}",
                relative.as_posix(),
            )
        )
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    closing = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if closing is None:
        diagnostics.append(
            StudioDiagnostic(
                "warning",
                "skill_frontmatter_error",
                "Skill frontmatter is missing a bounded closing delimiter",
                relative.as_posix(),
            )
        )
        return {}
    return _decode_skill_frontmatter(relative, "\n".join(lines[1:closing]), diagnostics)


def _decode_skill_frontmatter(
    relative: Path, source: str, diagnostics: list[StudioDiagnostic]
) -> dict[str, str]:
    """Keep only public string identity fields from a safe YAML mapping."""

    try:
        value = yaml.safe_load(source)
    except yaml.YAMLError:
        diagnostics.append(
            StudioDiagnostic(
                "warning",
                "skill_frontmatter_error",
                "Skill frontmatter is not valid YAML",
                relative.as_posix(),
            )
        )
        return {}
    if not isinstance(value, dict):
        return {}
    return _string_fields(value, ("name", "description"))


def _inspect_config(path: Path, state: _Inspection) -> None:
    """Project safe workspace identity fields and omit environment and secrets."""

    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        state.diagnostics.append(
            StudioDiagnostic(
                "error",
                "config_parse_error",
                f"Unable to parse config.yaml: {type(exc).__name__}",
                "config.yaml",
            )
        )
        return
    if not isinstance(value, dict):
        state.diagnostics.append(
            StudioDiagnostic(
                "error",
                "invalid_config",
                "config.yaml must contain a mapping",
                "config.yaml",
            )
        )
        return
    metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
    spec = value.get("spec") if isinstance(value.get("spec"), dict) else {}
    framework = spec.get("framework") if isinstance(spec.get("framework"), dict) else {}
    runtime = spec.get("runtime") if isinstance(spec.get("runtime"), dict) else {}
    config = _string_fields(metadata, ("name", "displayName"))
    config.update(_string_fields(spec, ("entrypoint",)))
    framework_fields = _string_fields(framework, ("name", "mode"))
    runtime_fields = _string_fields(runtime, ("version",))
    config.update({f"framework_{key}": item for key, item in framework_fields.items()})
    config.update({f"runtime_{key}": item for key, item in runtime_fields.items()})
    state.blocks.append(
        StudioBlock(
            "config.yaml:workspace",
            "workspace",
            str(config.get("name", path.parent.name)),
            "config.yaml",
            1,
            1,
            config,
        )
    )


def _string_fields(value: Mapping[str, Any], names: tuple[str, ...]) -> dict[str, str]:
    """Copy only public scalar identity fields from deployment configuration."""

    return {name: value[name] for name in names if isinstance(value.get(name), str)}


def _block_id(path: Path, kind: str, name: str) -> str:
    """Create a deterministic source-owned identifier independent of canvas layout."""

    return f"{path.as_posix()}:{kind}:{name}"


def _block_order(item: StudioBlock) -> tuple[str, int, str, str]:
    """Keep projections deterministic across filesystem enumeration orders."""

    return item.path, item.line, item.kind, item.id


def _connection_order(item: StudioConnection) -> tuple[str, str, str, int]:
    """Keep graph topology deterministic for hashing and clients."""

    return item.graph, item.source, item.target, item.line


def _diagnostic_order(item: StudioDiagnostic) -> tuple[str, int, str]:
    """Keep diagnostics stable for snapshots and user interfaces."""

    return item.path, item.line, item.code
