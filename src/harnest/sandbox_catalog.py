"""Compiler-scoped sandbox definitions, separate from agent execution authority."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterator, Mapping

from .sandbox import Sandbox


class SandboxCatalogError(ValueError):
    """A sandbox name is ambiguous or an agent requests an unknown declaration."""


@dataclass(frozen=True)
class _CatalogScope:
    """Keep folder ownership task-local during recursive graph compilation."""

    directory: Path
    declarations: Mapping[str, Sandbox]
    discovered: dict[Path, Mapping[str, Sandbox]]


_CURRENT: ContextVar[_CatalogScope | None] = ContextVar("harnest_sandbox_catalog", default=None)


def current_sandbox_catalog() -> Mapping[str, Sandbox]:
    """Return definitions available for explicit assignment, never an allowlist."""
    scope = _CURRENT.get()
    return scope.declarations if scope else MappingProxyType({})


@contextmanager
def independent_sandbox_catalog() -> Iterator[None]:
    """A public compile starts a new project boundary even inside another compile."""
    token = _CURRENT.set(None)
    try:
        yield
    finally:
        _CURRENT.reset(token)


@contextmanager
def sandbox_catalog_scope(
    directory: Path, discover: Callable[[Path], Mapping[str, Sandbox]],
) -> Iterator[None]:
    """Share ancestor definitions without granting access or allowing shadowing."""
    directory = directory.resolve()
    parent = _CURRENT.get()
    if parent is not None and directory == parent.directory:
        yield
        return
    inherited = parent.declarations if parent and directory.is_relative_to(parent.directory) else {}
    # Graphs may reference the same folder more than once. Reusing its exact
    # declarations preserves factory identity without weakening conflict checks.
    discovered = parent.discovered if parent else {}
    if directory not in discovered:
        discovered[directory] = MappingProxyType(dict(discover(directory / "sandbox")))
    local = discovered[directory]
    duplicates = inherited.keys() & local.keys()
    if duplicates:
        raise SandboxCatalogError(
            f"sandbox names in {directory / 'sandbox'} duplicate an ancestor declaration: "
            f"{', '.join(sorted(duplicates))}. Use a distinct filename and matching variable name; "
            "a child folder cannot silently replace a sandbox's security configuration."
        )
    token = _CURRENT.set(_CatalogScope(directory, MappingProxyType({**inherited, **local}), discovered))
    try:
        yield
    finally:
        _CURRENT.reset(token)


def resolve_sandbox_assignments(agent_name: str, names: tuple[str, ...]) -> Mapping[str, Sandbox]:
    """Grant exactly the authored names, failing closed on typos or missing files."""
    catalog = current_sandbox_catalog()
    missing = set(names) - catalog.keys()
    if missing:
        available = ", ".join(sorted(catalog)) or "none"
        raise SandboxCatalogError(
            f"agent {agent_name!r} assigns unknown sandboxes: {', '.join(sorted(missing))}. "
            f"Available names: {available}. Create sandbox/<name>.py with a matching Sandbox variable, "
            "or correct this agent's sandboxes=[...] list. Leaving that list empty grants no named sandboxes."
        )
    return MappingProxyType({name: catalog[name] for name in names})
