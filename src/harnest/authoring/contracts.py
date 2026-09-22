"""Typed declarations for trusted, explicitly installed project customisation packs."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from pydantic import BaseModel


class ProjectError(ValueError):
    """A project cannot be safely planned, validated, or changed."""


class WritePolicy(str, Enum):
    """Choose whether to fill a missing value or update pack-owned content."""
    IF_MISSING = "if_missing"
    MANAGED = "managed"


class ChangeKind(str, Enum):
    """Filesystem effects exposed in a reviewable project plan."""
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


@dataclass(frozen=True)
class _Operation:
    """One declarative mutation, interpreted only by the project planner."""
    kind: str
    path: str
    policy: WritePolicy = WritePolicy.IF_MISSING
    content: Any = None
    key: tuple[str, ...] = ()
    destination: tuple[str, ...] = ()


@dataclass(frozen=True, init=False)
class ChangePlan:
    """An ordered collection of file and YAML operations returned by a pack hook."""
    operations: tuple[_Operation, ...]

    def __init__(self, *operations: _Operation) -> None:
        """Capture operations without performing any filesystem writes."""
        if any(not isinstance(item, _Operation) for item in operations):
            raise ProjectError("ChangePlan accepts context.files/context.yaml operations only")
        object.__setattr__(self, "operations", operations)


@dataclass(frozen=True)
class _File:
    """Immutable source bytes and permission bits used for stale-plan validation."""
    content: bytes
    mode: int = 0o644


@dataclass(frozen=True)
class ProjectChange:
    """A path-level change summary which never exposes configuration contents."""
    kind: ChangeKind
    path: str
    owners: tuple[str, ...]


@dataclass(frozen=True)
class ProjectPlan:
    """A combined immutable snapshot plan; apply refuses blockers or changed inputs."""
    root: Path
    changes: tuple[ProjectChange, ...]
    blockers: tuple[str, ...]
    _before: Mapping[str, _File] = field(repr=False)
    _after: Mapping[str, _File] = field(repr=False)
    _root_existed: bool = field(repr=False)

    def public(self) -> dict[str, Any]:
        """Return a machine-readable preview without file contents or option values."""
        return {"apiVersion": "harnest.dev/v1alpha1", "kind": "ProjectPlan",
                "directory": str(self.root), "blockers": list(self.blockers),
                "changes": [{"kind": c.kind.value, "path": c.path, "owners": list(c.owners)}
                            for c in self.changes]}

    def render(self) -> str:
        """Render the same combined plan used by interactive and automated clients."""
        lines = [f"Project plan: {self.root}"]
        lines.extend(f"  {c.kind.value}: {c.path} ({', '.join(c.owners)})" for c in self.changes)
        lines.extend(f"  BLOCKED: {item}" for item in self.blockers)
        if not self.changes and not self.blockers:
            lines.append("  No changes required.")
        return "\n".join(lines) + "\n"


Hook = Callable[["ProjectContext"], ChangePlan]


class ProjectPack:
    """Company-owned scaffolding and consecutive schema migrations, never auto-discovered."""

    def __init__(self, name: str, schema_version: int, *, options: type[BaseModel] | None = None,
                 templates: str | Path | None = None, config_file: str | None = None,
                 config_model: type[BaseModel] | None = None) -> None:
        """Declare pack identity, optional typed inputs, templates and configuration validation."""
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", name) or name == "harnest":
            raise ProjectError("pack name must be a non-reserved kebab-case identifier")
        if type(schema_version) is not int or schema_version < 1:
            raise ProjectError("schema_version must be a positive integer")
        if (config_file is None) != (config_model is None):
            raise ProjectError("config_file and config_model must be supplied together")
        self.name, self.schema_version = name, schema_version
        self.options, self.templates = options, Path(templates) if templates is not None else None
        self.config_file, self.config_model = config_file, config_model
        self._initializer: Hook | None = None
        self._migrations: dict[int, Hook] = {}

    def initialize(self, function: Hook) -> Hook:
        """Register the single initializer used only when creating a new project."""
        if self._initializer is not None:
            raise ProjectError(f"{self.name}: initializer already registered")
        self._initializer = function
        return function

    def migration(self, *, from_version: int, to_version: int) -> Callable[[Hook], Hook]:
        """Register one consecutive migration; upgrades never rerun initialization."""
        if type(from_version) is not int or from_version < 1 or to_version != from_version + 1:
            raise ProjectError("migrations must advance one positive schema version")
        if to_version > self.schema_version or from_version in self._migrations:
            raise ProjectError("duplicate migration or destination exceeds pack schema")

        def register(function: Hook) -> Hook:
            """Bind the migration without executing authored code during registration."""
            if from_version in self._migrations:
                raise ProjectError("duplicate migration")
            self._migrations[from_version] = function
            return function
        return register


# Imported after declarations so operation builders can use the same exception type.
from .context import ProjectContext  # noqa: E402
