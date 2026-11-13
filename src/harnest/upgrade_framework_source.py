"""Rewrite reviewed framework APIs without importing or reformatting authored code."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
import io
import tokenize
from typing import Any

from .upgrade_framework_names import (
    ADAPTER_RENAMES, ADAPTER_TYPES, CAPABILITY_VALUE_RENAMES, CONSTRUCTORS,
    FIELD_RENAMES, GRAPH_TYPES, MCP_TYPES, METHOD_RENAMES,
    MODEL_TYPES, REMOVED_METHODS, REMOVED_SYMBOLS, RETIRED_MODULES,
    SERVER_KEYWORDS, STORE_TYPES, SYMBOL_RENAMES,
)

_AMBIGUOUS = "<harnest-ambiguous>"
_CHECKPOINTER = "harnest.frameworks.langgraph.checkpointer"
_BUILD_TYPES = MODEL_TYPES | GRAPH_TYPES
_CHANGED_METHODS = frozenset(METHOD_RENAMES) | frozenset(REMOVED_METHODS) | {"build", "connect"}


@dataclass(frozen=True)
class _Binding:
    """Track a lexical import or a receiver whose Harnest type is statically known."""

    qualified: str = ""
    owner: str = ""
    origin: ast.AST | None = None
    result: str = ""


@dataclass(frozen=True)
class _Edit:
    """Replace one original-source span; offsets never depend on prior replacements."""

    start: int
    end: int
    replacement: str


class _Source:
    """Map Python's UTF-8 AST columns to character offsets for surgical edits."""

    def __init__(self, source: str, path: str) -> None:
        """Index lines and tokens once without decoding argument values into diagnostics."""

        self.text = source
        self.path = path
        self.lines = source.splitlines(keepends=True)
        self.offsets = [0]
        for line in self.lines:
            self.offsets.append(self.offsets[-1] + len(line))
        self.tokens = tuple(tokenize.generate_tokens(io.StringIO(source).readline))
        self.edits: list[_Edit] = []

    def position(self, row: int, column: int, *, ast_column: bool = True) -> int:
        """Translate byte columns from AST or character columns from tokenize."""

        line = self.lines[row - 1]
        width = len(line.encode()[:column].decode()) if ast_column else column
        return self.offsets[row - 1] + width

    def span(self, node: ast.AST) -> tuple[int, int]:
        """Return the original character span of one syntax node."""

        return self.position(node.lineno, node.col_offset), self.position(node.end_lineno, node.end_col_offset)  # type: ignore[attr-defined]

    def segment(self, node: ast.AST) -> str:
        """Read one unchanged source fragment for a replacement expression."""

        start, end = self.span(node)
        return self.text[start:end]

    def replace(self, node: ast.AST, replacement: str) -> None:
        """Record a whole-node replacement without touching surrounding comments."""

        self.edits.append(_Edit(*self.span(node), replacement))

    def fail(self, node: ast.AST, reason: str) -> None:
        """Report the location and repair category without exposing authored values."""

        from .upgrade import UpgradeError

        raise UpgradeError(f"{self.path}:{getattr(node, 'lineno', 1)}: {reason}; review framework API migration manually")

    def attribute(self, node: ast.Attribute, name: str) -> None:
        """Replace only the terminal attribute name, preserving its receiver layout."""

        _, end = self.span(node)
        self.edits.append(_Edit(end - len(node.attr), end, name))

    def remove_argument(self, call: ast.Call, argument: ast.AST) -> None:
        """Remove a selector and its separator while retaining whitespace and comments."""

        start, end = self.span(argument)
        self.edits.append(_Edit(start, end, ""))
        call_start, call_end = self.span(call)
        commas = self._commas(call_start, call_end)
        following = next((position for position in commas if position >= end), None)
        next_start = self._next_argument_start(call, argument, end, call_end)
        if following is not None and following < next_start:
            self.edits.append(_Edit(following, following + 1, ""))
            return
        preceding = [position for position in commas if position < start]
        if preceding:
            self.edits.append(_Edit(preceding[-1], preceding[-1] + 1, ""))

    def _next_argument_start(self, call: ast.Call, argument: ast.AST, end: int, fallback: int) -> int:
        """Bound the removed selector's following separator to its own argument gap."""

        starts = [self.span(item)[0] for item in (*call.args, *call.keywords)
                  if item is not argument and self.span(item)[0] > end]
        return min(starts, default=fallback)

    def _commas(self, start: int, end: int) -> list[int]:
        """Locate separators within a call; argument spans exclude nested separators."""

        return [self.position(*token.start, ast_column=False) for token in self.tokens
                if token.type == tokenize.OP and token.string == ","
                and start <= self.position(*token.start, ast_column=False) < end]

    def render(self) -> str:
        """Apply non-overlapping edits in reverse order and reject ambiguous rewrites."""

        result = self.text
        previous = len(result) + 1
        unique = sorted(set(self.edits), key=lambda edit: (edit.start, edit.end, edit.replacement), reverse=True)
        for edit in unique:
            if edit.end > previous:
                from .upgrade import UpgradeError

                raise UpgradeError(f"{self.path}:1: overlapping framework API changes require manual review")
            result = result[:edit.start] + edit.replacement + result[edit.end:]
            previous = edit.start
        return result


def _canonical(name: str) -> str:
    """Normalize moved symbols for receiver inference while keeping original call identity."""

    return SYMBOL_RENAMES.get(name, name)


def _assigned_names(node: ast.AST) -> set[str]:
    """Collect bindings in this lexical scope without descending into nested scopes."""

    result: set[str] = set()
    children = (node.iter, *node.ifs) if isinstance(node, ast.comprehension) else ast.iter_child_nodes(node)
    for child in children:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            result.add(child.name)
        elif not isinstance(child, ast.Lambda):
            result.update(_direct_bindings(child))
            result.update(_assigned_names(child))
    return result


def _direct_bindings(node: ast.AST) -> set[str]:
    """Read import, assignment, and exception bindings without evaluating expressions."""

    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return {alias.asname or alias.name.split(".")[0] for alias in node.names}
    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        return {node.id}
    if isinstance(node, ast.ExceptHandler) and node.name:
        return {node.name}
    return set()


class _Migrator(ast.NodeVisitor):
    """Resolve explicit Harnest imports and receiver types within lexical scopes."""

    def __init__(
        self, source: _Source, framework: str, tree: ast.Module,
        imported_types: Mapping[str, str] | None = None, module: str = "",
    ) -> None:
        """Retain bindings and fresh import names separately from text modifications."""

        self.source = source
        self.framework = framework
        self.imported_types = imported_types or {}
        self.module = module
        self.bindings: dict[str, _Binding] = {}
        self.used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        self.used.update(alias.asname or alias.name.split(".")[0] for node in ast.walk(tree)
                         if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names)
        self.extra_imports: dict[tuple[ast.AST, str], str] = {}
        self.parents: list[ast.AST] = []
        self.class_outer: dict[str, _Binding] | None = None

    def visit(self, node: ast.AST) -> Any:
        """Track parents so signature-changing method references can fail closed."""

        self.parents.append(node)
        try:
            return super().visit(node)
        finally:
            self.parents.pop()

    def qualified(self, node: ast.AST) -> str:
        """Resolve module aliases without guessing types from variable spelling."""

        if isinstance(node, ast.Name):
            return self.bindings.get(node.id, _Binding()).qualified
        if isinstance(node, ast.Attribute):
            prefix = self.qualified(node.value)
            return f"{prefix}.{node.attr}" if prefix else ""
        return ""

    def owner(self, node: ast.AST) -> str:
        """Infer constructors, annotated names, aliases, and declarative factory results."""

        if isinstance(node, ast.Name):
            binding = self.bindings.get(node.id, _Binding())
            return binding.owner or self._constructor(binding.qualified)
        if isinstance(node, ast.Call):
            return self._call_owner(node)
        if isinstance(node, ast.Attribute):
            return self._constructor(self.qualified(node))
        if isinstance(node, ast.IfExp):
            owners = {self.owner(node.body), self.owner(node.orelse)}
            return next(iter(owners)) if len(owners) == 1 else self._ambiguous(owners)
        return ""

    def _constructor(self, name: str) -> str:
        """Recognize only reviewed Harnest constructors and imported authored objects."""

        canonical = _canonical(self.imported_types.get(name, name))
        if canonical in CONSTRUCTORS | ADAPTER_TYPES | {_AMBIGUOUS}:
            return canonical
        return _AMBIGUOUS if name.startswith(("harnest.lib.", "harnest.models.")) else ""

    def _call_owner(self, node: ast.Call) -> str:
        """Track model/MCP class factories without treating arbitrary methods as constructors."""

        if isinstance(node.func, ast.Name):
            returned = self.bindings.get(node.func.id, _Binding()).result
            if returned:
                return returned
        qualified = self.qualified(node.func)
        if qualified in {"harnest.frameworks.get_framework", "harnest.frameworks.current_framework"}:
            return "harnest.frameworks.FrameworkAdapter"
        direct = self._constructor(qualified)
        if direct and direct != _AMBIGUOUS:
            return direct
        prefix, _, method = qualified.rpartition(".")
        owner = self._constructor(prefix)
        if owner in MODEL_TYPES | MCP_TYPES and method in {
            "from_openai_environment", "streamable_http", "sse", "stdio", "openapi",
        }:
            return owner
        return direct

    def _ambiguous(self, owners: set[str]) -> str:
        """Retain uncertainty when a flow can produce a reviewed Harnest receiver."""

        return _AMBIGUOUS if owners & (CONSTRUCTORS | ADAPTER_TYPES | {_AMBIGUOUS}) else ""

    def _check_import(self, node: ast.AST, name: str) -> None:
        """Retired implementation modules and removed APIs need a human replacement."""

        if any(name == item or name.startswith(item + ".") for item in RETIRED_MODULES):
            self.source.fail(node, "retired framework implementation import")
        if name in REMOVED_SYMBOLS:
            self.source.fail(node, REMOVED_SYMBOLS[name])

    def visit_Import(self, node: ast.Import) -> None:
        """Record module aliases and reject old implementation imports before any edits."""

        for alias in node.names:
            self._check_import(node, alias.name)
            name = alias.asname or alias.name.split(".")[0]
            qualified = alias.name if alias.asname else name
            self.bindings[name] = _Binding(qualified, origin=node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Move reviewed symbols while preserving existing local alias bindings."""

        module = self._import_module(node)
        if node.level and not module:
            for alias in node.names:
                self.bindings[alias.asname or alias.name] = _Binding()
            return
        self._check_import(node, module)
        transformed = [self._import_alias(node, module, alias) for alias in node.names]
        if any(destination != module for destination, _ in transformed):
            self._split_import(node, transformed)
        else:
            for alias, (_, replacement) in zip(node.names, transformed):
                if replacement != self.source.segment(alias):
                    self.source.replace(alias, replacement)

    def _import_module(self, node: ast.ImportFrom) -> str:
        """Resolve relative authored imports from a supplied module identity only."""

        if not node.level:
            return node.module or ""
        parts = self.module.split(".")
        if not self.module or node.level >= len(parts):
            return ""
        return ".".join((*parts[:-node.level], *((node.module or "").split(".") if node.module else ())))

    def _import_alias(self, node: ast.ImportFrom, module: str, alias: ast.alias) -> tuple[str, str]:
        """Describe one import relocation without rewriting its later references."""

        if alias.name == "*" and module.startswith("harnest"):
            self.source.fail(node, "wildcard Harnest import obscures changed API ownership")
        qualified = f"{module}.{alias.name}"
        self._check_import(node, qualified)
        self.bindings[alias.asname or alias.name] = _Binding(qualified, origin=node)
        if qualified == _CHECKPOINTER:
            return "", ""
        target = SYMBOL_RENAMES.get(qualified)
        if target is None:
            return module, self.source.segment(alias)
        destination, _, name = target.rpartition(".")
        local = alias.asname or alias.name
        replacement = name if name == local else f"{name} as {local}"
        return destination, replacement

    def _split_import(self, node: ast.ImportFrom, aliases: list[tuple[str, str]]) -> None:
        """Split moved modules while retaining import comments and all local names."""

        indent = " " * node.col_offset
        statements = [f"from {module} import {alias}" for module, alias in aliases if module]
        start, end = self.source.span(node)
        comments = [token.string for token in self.source.tokens if token.type == tokenize.COMMENT
                    and start <= self.source.position(*token.start, ast_column=False) < end]
        separator = "; " if node.lineno == node.end_lineno else "\n" + indent
        replacement = separator.join(statements) or "pass"
        if comments:
            suffix = self.source.lines[node.end_lineno - 1].encode()[node.end_col_offset:].decode()
            if suffix.lstrip().startswith(";"):
                self.source.fail(node, "commented moved import shares a line with executable statements")
            replacement += "\n" + indent + ("\n" + indent).join(comments)
        self.source.replace(node, replacement)

    def _origin(self, node: ast.AST) -> ast.AST | None:
        """Find the lexical module import that owns an attributed symbol."""

        while isinstance(node, ast.Attribute):
            node = node.value
        return self.bindings.get(node.id, _Binding()).origin if isinstance(node, ast.Name) else None

    def _import_reference(self, node: ast.Attribute, target: str) -> None:
        """Add moved imports beside their original scope instead of importing globally."""

        origin = self._origin(node)
        if origin is None:
            self.source.fail(node, "unresolved Harnest symbol owner")
        key = (origin, target)
        alias = self.extra_imports.get(key)
        if alias is None:
            module, _, name = target.rpartition(".")
            alias = f"_harnest_framework_{name}"
            while alias in self.used:
                alias += "_"
            self.used.add(alias)
            self.extra_imports[key] = alias
            _, end = self.source.span(origin)
            self.source.edits.append(_Edit(end, end, f"; from {module} import {name} as {alias}"))
        self.source.replace(node, alias)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Rename module-qualified symbols and fields only on proven Harnest receivers."""

        qualified = self.qualified(node)
        self._check_import(node, qualified)
        if qualified == _CHECKPOINTER:
            self.source.fail(node, "checkpoint helper captured as a callable")
        if qualified in SYMBOL_RENAMES:
            target = SYMBOL_RENAMES[qualified]
            if qualified.rpartition(".")[0] == target.rpartition(".")[0]:
                self.source.attribute(node, target.rpartition(".")[2])
            else:
                self._import_reference(node, target)
            return
        self._receiver_attribute(node)
        self.visit(node.value)

    def _receiver_attribute(self, node: ast.Attribute) -> None:
        """Reject uncertain migrations and apply fields or simple method renames."""

        owner = self.owner(node.value)
        self._check_receiver(node, owner)
        if node.attr in FIELD_RENAMES.get(owner, {}):
            self.source.attribute(node, FIELD_RENAMES[owner][node.attr])
        if owner in MCP_TYPES and node.attr == "to_langgraph_connection":
            self.source.attribute(node, "connection_options")
        if owner in STORE_TYPES and node.attr == "as_langgraph_checkpointer":
            self.source.attribute(node, "framework_checkpointer")
        if owner in ADAPTER_TYPES and node.attr in ADAPTER_RENAMES:
            self.source.attribute(node, ADAPTER_RENAMES[node.attr])

    def _parent_call(self, node: ast.AST) -> ast.Call | None:
        """Identify the call whose function is the visited expression."""

        parent = self.parents[-2] if len(self.parents) > 1 else None
        return parent if isinstance(parent, ast.Call) and parent.func is node else None

    def _check_receiver(self, node: ast.Attribute, owner: str) -> None:
        """Avoid rewriting ambiguous receivers or capturing changed builder signatures."""

        call = self._parent_call(node)
        unchanged = self._unchanged_call(node, call)
        if owner == _AMBIGUOUS and node.attr in _CHANGED_METHODS and not unchanged:
            self.source.fail(node, "ambiguous Harnest receiver for a changed method")
        if node.attr in REMOVED_METHODS and owner in CONSTRUCTORS:
            self.source.fail(node, REMOVED_METHODS[node.attr])
        if call is None and owner in _BUILD_TYPES and node.attr in {"build_for", "build_langgraph"}:
            self.source.fail(node, "changed builder captured as a callable")

    def _unchanged_call(self, node: ast.Attribute, call: ast.Call | None) -> bool:
        """Unchanged no-argument helpers do not need receiver inference."""

        return node.attr in {"build", "connect"} and call is not None and not (call.args or call.keywords)

    def visit_Call(self, node: ast.Call) -> None:
        """Migrate calls through symbol identity or a statically inferred receiver."""

        qualified = self.qualified(node.func)
        self._check_import(node, qualified)
        if qualified == _CHECKPOINTER:
            self._checkpoint_call(node)
            return
        self._function_call(node, qualified)
        if isinstance(node.func, ast.Attribute):
            self._method_call(node, self.owner(node.func.value))
        self.generic_visit(node)

    def _function_call(self, node: ast.Call, qualified: str) -> None:
        """Handle framework-aware free functions and renamed constructor keywords."""

        canonical = _canonical(qualified)
        if qualified in {"harnest.model.resolve_model_for", "harnest.model.resolve_model"}:
            self._selector(node, 1, {"framework"}, required=qualified.endswith("_for"))
        if qualified == "harnest.bundle.bundle_agent":
            self._selector(node, None, {"framework"})
        if canonical == "harnest.runtime.create_fastapi_app":
            self._keywords(node, SERVER_KEYWORDS)
        if canonical in FIELD_RENAMES:
            self._keywords(node, FIELD_RENAMES[canonical])
        if canonical in {"harnest.frameworks.Capability", "harnest.frameworks.base.Capability"}:
            self._capability_call(node)

    def _capability_call(self, node: ast.Call) -> None:
        """Migrate enum value literals only where their capability meaning is explicit."""

        candidates = [*node.args, *(item.value for item in node.keywords if item.arg == "value")]
        if len(candidates) != 1 or not isinstance(candidates[0], ast.Constant):
            self.source.fail(node, "dynamic capability construction needs manual adaptation")
        value = candidates[0]
        if isinstance(value.value, str) and value.value in CAPABILITY_VALUE_RENAMES:
            original = self.source.segment(value)
            renamed = CAPABILITY_VALUE_RENAMES[value.value]
            replacement = original.replace(value.value, renamed) if value.value in original else repr(renamed)
            self.source.replace(value, replacement)

    def _method_call(self, node: ast.Call, owner: str) -> None:
        """Rewrite only the methods supported by the receiver's reviewed public type."""

        method = node.func.attr  # type: ignore[attr-defined]
        if owner in _BUILD_TYPES and method in {"build", "build_for", "build_langgraph"}:
            self._builder(node, owner, method)
        elif owner in MCP_TYPES and method == "connect":
            self._selector(node, None, {"framework"})
        elif owner in MCP_TYPES and method == "to_langgraph_connection":
            self.source.attribute(node.func, "connection_options")  # type: ignore[arg-type]
        elif owner in STORE_TYPES and method == "as_langgraph_checkpointer":
            self.source.attribute(node.func, "framework_checkpointer")  # type: ignore[arg-type]
        elif owner in ADAPTER_TYPES:
            self._keywords(node, ADAPTER_RENAMES)

    def _builder(self, node: ast.Call, owner: str, method: str) -> None:
        """Drop only validated selectors and retain the configured framework's meaning."""

        if method == "build_langgraph":
            if self.framework != "langgraph" or node.args or node.keywords:
                self.source.fail(node, "framework-specific model builder conflicts with project selection")
        else:
            names = {"framework", "backend"} if owner in GRAPH_TYPES else {"framework"}
            self._selector(node, 0, names, required=method == "build_for")
        if method != "build":
            self.source.attribute(node.func, "build")  # type: ignore[arg-type]

    def _selector(self, node: ast.Call, position: int | None, names: set[str], *, required: bool = False) -> None:
        """Reject dynamic, conflicting, or mismatched framework selectors before removal."""

        candidates: list[ast.AST] = [item for item in node.keywords if item.arg in names]
        if position is not None and len(node.args) > position:
            candidates.append(node.args[position])
        self._explicit_arguments(node)
        if len(candidates) > 1 or (required and not candidates):
            self.source.fail(node, "missing or conflicting framework selector")
        if candidates:
            self._selected_literal(candidates[0])
            self.source.remove_argument(node, candidates[0])

    def _explicit_arguments(self, node: ast.Call) -> None:
        """Argument expansion can conceal duplicate selectors or renamed keywords."""

        if any(item.arg is None for item in node.keywords) or any(isinstance(item, ast.Starred) for item in node.args):
            self.source.fail(node, "dynamic argument expansion obscures framework selection")

    def _selected_literal(self, selector: ast.AST) -> None:
        """Compare only a literal selector, never evaluate authored expressions."""

        value = selector.value if isinstance(selector, ast.keyword) else selector
        if not isinstance(value, ast.Constant) or value.value != self.framework:
            self.source.fail(selector, "dynamic or mismatched framework selector")

    def _keywords(self, node: ast.Call, names: dict[str, str]) -> None:
        """Rename known keyword slots while rejecting duplicate or hidden destinations."""

        changed = [item for item in node.keywords if item.arg in names]
        if not changed:
            return
        destinations = [names.get(item.arg, item.arg) for item in node.keywords]
        if None in destinations or len(destinations) != len(set(destinations)):
            self.source.fail(node, "conflicting or dynamic renamed keyword arguments")
        for keyword in changed:
            start, _ = self.source.span(keyword)
            self.source.edits.append(_Edit(start, start + len(keyword.arg or ""), names[keyword.arg]))  # type: ignore[index]

    def _checkpoint_call(self, node: ast.Call) -> None:
        """Replace the transient package helper while keeping argument comments intact."""

        if len(node.args) != 1 or node.keywords:
            self.source.fail(node, "checkpoint helper requires one explicit store")
        self.source.replace(node.func, "")
        _, end = self.source.span(node)
        _, argument_end = self.source.span(node.args[0])
        for comma in self.source._commas(argument_end, end):
            self.source.edits.append(_Edit(comma, comma + 1, ""))
        self.source.edits.append(_Edit(end, end, ".framework_checkpointer()"))

    def visit_Name(self, node: ast.Name) -> None:
        """Do not leave references to the removed standalone checkpoint helper."""

        if self.qualified(node) == _CHECKPOINTER and self._parent_call(node) is None:
            self.source.fail(node, "checkpoint helper captured as a callable")

    def _bind(self, target: ast.AST, value: ast.AST | None, annotation: ast.AST | None = None) -> None:
        """Track assignments without carrying a shadowed module alias into a new value."""

        if not isinstance(target, ast.Name):
            return
        owner = self.owner(value) if value is not None else ""
        if annotation is not None:
            owner = self._annotation(annotation) or owner
        qualified = self.qualified(value) if isinstance(value, (ast.Name, ast.Attribute)) else ""
        origin = self._origin(value) if value is not None else None
        self.bindings[target.id] = _Binding(qualified, owner, origin)

    def _annotation(self, annotation: ast.AST) -> str:
        """Resolve direct and optional annotations without evaluating authored type expressions."""

        owner = self._constructor(self.qualified(annotation))
        if owner:
            return owner
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            return self._forward_annotation(annotation.value)
        if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
            return self._annotation_union((annotation.left, annotation.right))
        if isinstance(annotation, ast.Subscript):
            return self._annotation_subscript(annotation)
        return ""

    def _forward_annotation(self, annotation: str) -> str:
        """Parse forward types as syntax only, preserving the authored string itself."""

        try:
            expression = ast.parse(annotation, mode="eval").body
        except SyntaxError:
            return ""
        return self._annotation(expression)

    def _annotation_union(self, annotations: tuple[ast.AST, ...]) -> str:
        """Optional types preserve their receiver; mixed types remain ambiguous."""

        members = [item for item in annotations if not (isinstance(item, ast.Constant) and item.value is None)]
        owners = {self._annotation(item) for item in members}
        return next(iter(owners)) if len(owners) == 1 else self._ambiguous(owners)

    def _annotation_subscript(self, annotation: ast.Subscript) -> str:
        """Recognize standard Optional and Union wrappers, not arbitrary generic values."""

        name = self.qualified(annotation.value)
        if name in {"typing.Optional", "typing.Annotated"}:
            value = annotation.slice.elts[0] if isinstance(annotation.slice, ast.Tuple) else annotation.slice
            return self._annotation(value)
        if name == "typing.Union" and isinstance(annotation.slice, ast.Tuple):
            return self._annotation_union(tuple(annotation.slice.elts))
        return ""

    def visit_Assign(self, node: ast.Assign) -> None:
        """Visit the old value before rebinding aliases to the assigned receiver type."""

        self.visit(node.value)
        for target in node.targets:
            self.visit(target)
            if not isinstance(target, ast.Name):
                self._clear_target(target)
            self._bind(target, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """Use explicit receiver annotations even when an authored factory is opaque."""

        self.generic_visit(node)
        self._bind(node.target, node.value, node.annotation)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """Respect parameter/local shadowing while retaining imported outer definitions."""

        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        returned = self._annotation(node.returns) if node.returns is not None else ""
        parameters = self._argument_bindings(node.args)
        outer = self.bindings
        class_outer = self.class_outer
        lexical = outer if class_outer is None else class_outer
        self.bindings = {**lexical, **{name: _Binding() for name in _assigned_names(node)}, **parameters}
        self.class_outer = None
        try:
            if node.returns is not None:
                self.visit(node.returns)
            for statement in node.body:
                self.visit(statement)
        finally:
            self.bindings = outer
            self.class_outer = class_outer
        self.bindings[node.name] = _Binding(result=returned)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_For(self, node: ast.For | ast.AsyncFor) -> None:
        """Loop targets shadow imports; zero iterations preserve an uncertain prior type."""

        self.visit(node.iter)
        before = dict(self.bindings)
        self._clear_target(node.target)
        for statement in node.body:
            self.visit(statement)
        self.bindings = self._merge(before, self.bindings)
        for statement in node.orelse:
            self.visit(statement)

    visit_AsyncFor = visit_For

    def visit_While(self, node: ast.While) -> None:
        """Do not assume a loop executes when propagating receiver identities."""

        self.visit(node.test)
        before = dict(self.bindings)
        for statement in node.body:
            self.visit(statement)
        self.bindings = self._merge(before, self.bindings)
        for statement in node.orelse:
            self.visit(statement)

    def visit_Try(self, node: ast.Try | ast.TryStar) -> None:
        """Merge exception paths before following unconditional cleanup statements."""

        before = dict(self.bindings)
        for statement in (*node.body, *node.orelse):
            self.visit(statement)
        merged = self.bindings
        for handler in node.handlers:
            self.bindings = dict(before)
            self.visit(handler)
            merged = self._merge(merged, self.bindings)
        self.bindings = merged
        for statement in node.finalbody:
            self.visit(statement)

    visit_TryStar = visit_Try

    def visit_With(self, node: ast.With | ast.AsyncWith) -> None:
        """Context manager results replace earlier aliases before visiting their bodies."""

        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._clear_target(item.optional_vars)
        for statement in node.body:
            self.visit(statement)

    visit_AsyncWith = visit_With

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        """Exception targets are unrelated to any imported name they shadow."""

        if node.type is not None:
            self.visit(node.type)
        if node.name:
            self.bindings[node.name] = _Binding()
        for statement in node.body:
            self.visit(statement)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        """Keep lambda parameters from inheriting unrelated outer receiver types."""

        outer = self.bindings
        class_outer = self.class_outer
        self.bindings = dict(outer if class_outer is None else class_outer)
        self.class_outer = None
        try:
            self.bindings.update(self._argument_bindings(node.args))
            self.visit(node.body)
        finally:
            self.bindings = outer
            self.class_outer = class_outer

    def visit_ListComp(self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp) -> None:
        """Respect comprehension-local binding and iteration order."""

        outer = self.bindings
        self.bindings = dict(outer)
        try:
            for generator in node.generators:
                self.visit(generator.iter)
                self._clear_target(generator.target)
                for condition in generator.ifs:
                    self.visit(condition)
            self._comprehension_value(node)
        finally:
            self.bindings = outer

    visit_SetComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp
    visit_DictComp = visit_ListComp

    def _comprehension_value(self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp) -> None:
        """Visit result expressions after their local targets have been bound."""

        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)

    def _clear_target(self, node: ast.AST) -> None:
        """Unknown iteration or destructuring values must shadow prior imports."""

        for target in ast.walk(node):
            if isinstance(target, ast.Name) and isinstance(target.ctx, ast.Store):
                self.bindings[target.id] = _Binding()

    def _argument_bindings(self, arguments: ast.arguments) -> dict[str, _Binding]:
        """Resolve annotations in their definition scope before parameters shadow names."""

        parameters = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
        parameters += [item for item in (arguments.vararg, arguments.kwarg) if item is not None]
        resolved: dict[str, _Binding] = {}
        for parameter in parameters:
            owner = self._annotation(parameter.annotation) if parameter.annotation is not None else ""
            resolved[parameter.arg] = _Binding(owner=owner)
            if parameter.annotation is not None:
                self.visit(parameter.annotation)
        return resolved

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Keep unrelated classes intact and reject incompatible custom connector overrides."""

        connector = any(_canonical(self.qualified(base)) in MODEL_TYPES for base in node.bases)
        if connector:
            self._connector_class(node)
        if any(_canonical(self.qualified(base)) in ADAPTER_TYPES for base in node.bases):
            self._adapter_class(node)
        for expression in (*node.bases, *node.decorator_list):
            self.visit(expression)
        outer = self.bindings
        class_outer = self.class_outer
        lexical = outer if class_outer is None else class_outer
        self.class_outer = lexical
        self.bindings = dict(lexical)
        try:
            for statement in node.body:
                self.visit(statement)
        finally:
            self.bindings = outer
            self.class_outer = class_outer
        self.bindings[node.name] = _Binding(result="harnest.model.ModelConnector" if connector else "")

    def _connector_class(self, node: ast.ClassDef) -> None:
        """Do not collapse custom framework implementations into duplicate build methods."""

        for statement in node.body:
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if statement.name in {"build_for", "build_langgraph"}:
                self.source.fail(statement, "custom ModelConnector override needs manual adaptation")
            if statement.name == "build" and self._custom_builder_arguments(statement.args):
                self.source.fail(statement, "custom ModelConnector build signature needs manual adaptation")

    def _custom_builder_arguments(self, arguments: ast.arguments) -> bool:
        """Custom model builders must accept only their bound receiver after migration."""

        return (len(arguments.args) + len(arguments.posonlyargs) > 1 or bool(arguments.kwonlyargs)
                or arguments.vararg is not None or arguments.kwarg is not None)

    def _adapter_class(self, node: ast.ClassDef) -> None:
        """Custom adapter overrides must be reconciled with the current hook contract."""

        for statement in node.body:
            names = _assigned_names(statement)
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(statement.name)
                names.update(item.arg for item in (*statement.args.args, *statement.args.kwonlyargs))
            if names & ADAPTER_RENAMES.keys():
                self.source.fail(statement, "custom FrameworkAdapter hook needs manual adaptation")

    def visit_If(self, node: ast.If) -> None:
        """Merge branch receiver types conservatively instead of trusting visit order."""

        self.visit(node.test)
        original = dict(self.bindings)
        for statement in node.body:
            self.visit(statement)
        left = dict(self.bindings)
        self.bindings = dict(original)
        for statement in node.orelse:
            self.visit(statement)
        right = self.bindings
        self.bindings = self._merge(left, right)

    def _merge(self, left: dict[str, _Binding], right: dict[str, _Binding]) -> dict[str, _Binding]:
        """Retain only agreeing aliases; Harnest/unknown unions require review on use."""

        result: dict[str, _Binding] = {}
        for name in left.keys() | right.keys():
            first, second = left.get(name, _Binding()), right.get(name, _Binding())
            if first == second:
                result[name] = first
            else:
                owner = self._ambiguous({first.owner or self._constructor(first.qualified),
                                         second.owner or self._constructor(second.qualified)})
                result[name] = _Binding(owner=owner)
        return result


class _ExportCollector(_Migrator):
    """Reuse lexical inference while a cross-file index is incomplete and never edit."""

    def visit_Import(self, node: ast.Import) -> None:
        """Record import bindings without migration checks during fixed-point discovery."""

        for alias in node.names:
            local = alias.asname or alias.name.split(".")[0]
            self.bindings[local] = _Binding(alias.name if alias.asname else local)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Resolve explicit local re-exports without importing their modules."""

        module = self._import_module(node)
        for alias in node.names:
            qualified = f"{module}.{alias.name}" if module else ""
            self.bindings[alias.asname or alias.name] = _Binding(qualified)

    def visit_Name(self, node: ast.Name) -> None:
        """Names carry no independent binding effects during discovery."""

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Read expressions without applying migration-specific attribute changes."""

        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """Inspect argument bindings without invoking or migrating authored calls."""

        self.generic_visit(node)

    def _connector_class(self, node: ast.ClassDef) -> None:
        """Defer legacy connector validation until the migration pass."""

    def _adapter_class(self, node: ast.ClassDef) -> None:
        """Defer legacy adapter validation until the migration pass."""


def _parse_framework_source(source: str, path: str) -> tuple[ast.Module, _Source]:
    """Parse authored syntax with location-only errors shared by both static passes."""

    from .upgrade import UpgradeError

    try:
        return ast.parse(source), _Source(source, path)
    except (SyntaxError, tokenize.TokenError, UnicodeError) as error:
        line = getattr(error, "lineno", None) or 1
        raise UpgradeError(f"{path}:{line}: invalid Python source requires manual framework migration") from None


def framework_source_exports(
    source: str, *, imported_types: Mapping[str, str] | None = None,
    module: str = "", path: str = "<source>",
) -> dict[str, str]:
    """Infer module exports from syntax and a caller-owned cross-file type index.

    Values name reviewed Harnest types. Conflicting Harnest ownership uses the
    opaque ``<harnest-ambiguous>`` marker so ordinary imports also fail closed;
    exports with no evidence of Harnest ownership are omitted.
    Pass the source's dotted module name to resolve relative imports. For package
    initializers include ``.__init__`` so its sibling imports use the package itself.
    No authored imports, constructors, factories, or annotations are executed.
    """

    tree, indexed = _parse_framework_source(source, path)
    collector = _ExportCollector(indexed, "", tree, imported_types, module)
    collector.visit(tree)
    result: dict[str, str] = {}
    for name, binding in collector.bindings.items():
        owner = binding.owner or binding.result or collector._constructor(binding.qualified)
        if owner in CONSTRUCTORS | ADAPTER_TYPES | {_AMBIGUOUS}:
            result[name] = owner
    return result


def migrate_framework_source(
    source: str, *, framework: str, path: str,
    imported_types: Mapping[str, str] | None = None, module: str = "",
) -> str:
    """Migrate proven Harnest references, optionally using a statically built type index."""

    from .upgrade import UpgradeError

    tree, indexed = _parse_framework_source(source, path)
    _Migrator(indexed, framework, tree, imported_types, module).visit(tree)
    result = indexed.render()
    try:
        ast.parse(result)
    except SyntaxError:
        raise UpgradeError(f"{path}:1: framework rewrite needs manual syntax review") from None
    return result
