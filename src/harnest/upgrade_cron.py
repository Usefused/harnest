"""Explain optional cron migrations without changing durable schedule identities."""

from __future__ import annotations

import ast
from pathlib import Path

from .upgrade import (
    _authoring_python_files, _dotted_name, _namespace_prefixes,
)


def cron_migration_notes(root: Path) -> tuple[str, ...]:
    """Find supported Cron constructors at their current locations without importing code."""

    notes = []
    for path in _authoring_python_files(root):
        # The main source planner already reports unreadable or invalid files;
        # advisory discovery must neither follow links nor duplicate blockers.
        if path.is_symlink():
            continue
        try:
            # Locations refer to the files the user can review now, before
            # namespace rewrites potentially split imports into extra lines.
            source = path.read_text(encoding="utf-8")
            module = ast.parse(source, filename=str(path))
        except (OSError, UnicodeError, SyntaxError):
            continue
        names = _constructor_names(module)
        locations = sorted({
            node.lineno for node in ast.walk(module)
            if isinstance(node, ast.Call) and _dotted_name(node.func) in names
        })
        notes.extend(_migration_note(path.relative_to(root), line) for line in locations)
    return tuple(notes)


def _constructor_names(module: ast.Module) -> set[str]:
    """Recognize public and legacy import aliases while ignoring unrelated Cron types."""

    names = {f"{prefix}.Cron" for prefix in _namespace_prefixes(module, "cron")}
    for node in module.body:
        if isinstance(node, ast.ImportFrom) and node.module in {"harnest", "harnest.cron"}:
            names.update(alias.asname or alias.name for alias in node.names if alias.name == "Cron")
    return names


def _migration_note(path: Path, line: int) -> str:
    """Give a supported alternative and disclose the runtime identity change."""

    return (
        f"{path.as_posix()}:{line}: Cron(...) remains supported; no automatic rewrite is required. "
        "For work owned by one schedule, define @cron(expression, arguments=..., "
        "queue=..., max_retries=...) on the matching function in cron/<name>.py; "
        "use @cron() with cron.create(task=\"<name>\", ...) for dynamic schedules. "
        "Preserve the existing task's queue, retries, arguments and callable behavior. "
        "Conversion changes the task identity: fixed schedules are replaced with a new "
        "next-run cursor, and existing dynamic jobs still target the old task. "
        "Keep tasks used by other callers or retained jobs; review the rollout before removing them. "
        "See https://usefused.com/docs/harnest/build/scheduled-tasks#migrate-an-existing-cron-declaration"
    )
