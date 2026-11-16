"""Point out retired framework API names that harnest upgrade could not trace and left in place.

The source migration rewrites only references it can prove belong to Harnest.
Calls on untyped parameters, ``getattr`` lookups, and keyword arguments to
functions it cannot resolve stay unchanged, and would fail at compile or run
time. These notes list them for a person to check; they never block upgrade.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping

from .upgrade_framework_names import (
    ADAPTER_RENAMES,
    FIELD_RENAMES,
    METHOD_RENAMES,
    REMOVED_METHODS,
    REMOVED_SYMBOLS,
    SERVER_KEYWORDS,
    SYMBOL_RENAMES,
)

# Names too generic to report without type evidence; the migrator already
# blocks their only Harnest spelling (the import or the typed receiver).
_GENERIC = frozenset({"Backend", "native"})
_DYNAMIC_LOOKUPS = frozenset({"getattr", "hasattr", "setattr", "delattr"})


def _last(dotted: str) -> str:
    return dotted.rsplit(".", 1)[-1]


def _retired_attributes() -> dict[str, str]:
    """Map each retired attribute or function name to what replaces it."""

    hints: dict[str, str] = {}
    for old, new in SYMBOL_RENAMES.items():
        if _last(old) != _last(new):
            hints[_last(old)] = f"now `{_last(new)}`"
    for renames in (METHOD_RENAMES, ADAPTER_RENAMES, *FIELD_RENAMES.values()):
        hints.update({old: f"now `{new}`" for old, new in renames.items()})
    hints.update(REMOVED_METHODS)
    hints.update({_last(old): reason for old, reason in REMOVED_SYMBOLS.items()})
    return {name: hint for name, hint in hints.items() if name not in _GENERIC}


RETIRED_ATTRIBUTES: Mapping[str, str] = _retired_attributes()
RETIRED_KEYWORDS: Mapping[str, str] = {
    old: f"now `{new}=`" for old, new in SERVER_KEYWORDS.items()
}


def framework_review_notes(source: str, path: str) -> list[str]:
    """Return one note per retired name still used in already-migrated ``source``."""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []  # The migrator reports invalid syntax as a blocker.
    defined = _defined_names(tree)
    first: dict[str, tuple[int, int, str]] = {}
    for name, line, hint in _findings(tree):
        if name in defined:
            continue  # The project defines this name itself.
        seen_line, count, _ = first.get(name, (line, 0, hint))
        first[name] = (min(seen_line, line), count + 1, hint)
    return [
        f"{path}:{line}: `{name}` ({count} use{'s' if count > 1 else ''}) may be a "
        f"retired Harnest API that upgrade could not trace; {hint}"
        for name, (line, count, hint) in sorted(first.items(), key=lambda item: item[1][0])
    ]


def _findings(tree: ast.AST) -> Iterator[tuple[str, int, str]]:
    """Yield attribute uses, dynamic lookups, and keywords that use retired names."""

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in RETIRED_ATTRIBUTES:
            if not _own_receiver(node):
                yield node.attr, node.lineno, RETIRED_ATTRIBUTES[node.attr]
        elif isinstance(node, ast.Call):
            yield from _call_findings(node)


def _call_findings(node: ast.Call) -> Iterator[tuple[str, int, str]]:
    """Check ``getattr(obj, "name")`` lookups and keyword arguments of one call."""

    if isinstance(node.func, ast.Name) and node.func.id in _DYNAMIC_LOOKUPS and len(node.args) >= 2:
        name = node.args[1]
        if isinstance(name, ast.Constant) and name.value in RETIRED_ATTRIBUTES:
            yield name.value, node.lineno, RETIRED_ATTRIBUTES[name.value]
    for keyword in node.keywords:
        if keyword.arg in RETIRED_KEYWORDS:
            yield keyword.arg, keyword.value.lineno, RETIRED_KEYWORDS[keyword.arg]


def _own_receiver(node: ast.Attribute) -> bool:
    """``self.x`` and ``cls.x`` belong to the class being written, not to Harnest."""

    return isinstance(node.value, ast.Name) and node.value.id in {"self", "cls"}


def _defined_names(tree: ast.AST) -> set[str]:
    """Collect functions, classes, and attributes this file defines for itself."""

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            names.add(node.attr)
    return names
