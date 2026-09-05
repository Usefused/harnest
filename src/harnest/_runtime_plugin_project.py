"""Static PEP 621 metadata for same-interpreter runtime plugins."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 remains inside Harnest's supported range.
    import tomli as tomllib

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version


_MAX_PROJECT_BYTES = 1024 * 1024


class RuntimePluginProjectError(ValueError):
    """A plugin project cannot safely join the agent dependency environment."""


@dataclass(frozen=True, slots=True)
class RuntimePluginProject:
    """Validated plugin identity and deterministic runtime requirements."""

    name: str
    version: str
    dependencies: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResolvedDependency:
    """One de-duplicated requirement with every authored owner retained."""

    name: str
    requirement: str
    owners: tuple[str, ...]


def load_runtime_plugin_project(
    directory: Path,
    *,
    expected_name: str,
    expected_version: str,
    canonical_extension: bool = False,
) -> RuntimePluginProject:
    """Load an optional plugin pyproject without importing plugin code.

    Canonical Harnest Extensions use a prefixed distribution name so their
    package identity cannot collide with a provider SDK such as ``docker``.
    Legacy runtime plugins retain their original exact-name contract.
    """

    path = directory / "pyproject.toml"
    if not path.exists() and not path.is_symlink():
        return RuntimePluginProject(expected_name, expected_version, ())
    project = _load_project(path, owner=f"runtime plugin {expected_name!r}")
    _require_matching_identity(
        project,
        expected_name,
        expected_version,
        path,
        canonical_extension=canonical_extension,
    )
    return RuntimePluginProject(
        name=str(project["name"]),
        version=str(project["version"]),
        dependencies=_project_dependencies(project, path),
    )


def resolve_runtime_plugin_dependencies(
    agent_pyproject: Path | None,
    projects: Sequence[tuple[str, RuntimePluginProject]],
) -> tuple[ResolvedDependency, ...]:
    """Merge root and plugin requirements with stable ownership diagnostics."""

    declarations: list[tuple[str, str, str]] = []
    if agent_pyproject is not None and agent_pyproject.exists():
        project = _load_project(agent_pyproject, owner="agent")
        declarations.extend(
            (name, requirement, "agent pyproject.toml")
            for name, requirement in _dependency_items(project, agent_pyproject)
        )
    for plugin_name, project in projects:
        declarations.extend(
            (name, requirement, f"runtime plugin {plugin_name!r} pyproject.toml")
            for name, requirement in _requirement_items(project.dependencies)
        )
    return _merge_declarations(declarations)


def _load_project(path: Path, *, owner: str) -> dict[str, object]:
    """Decode one bounded regular TOML file and require a PEP 621 project table."""

    try:
        info = path.lstat()
        if path.is_symlink() or not path.is_file():
            raise RuntimePluginProjectError(
                f"{owner} pyproject.toml must be a regular file: {path}"
            )
        if info.st_size > _MAX_PROJECT_BYTES:
            raise RuntimePluginProjectError(
                f"{owner} pyproject.toml exceeds {_MAX_PROJECT_BYTES} bytes: {path}"
            )
        document = tomllib.loads(path.read_text("utf-8"))
    except RuntimePluginProjectError:
        raise
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise RuntimePluginProjectError(
            f"cannot read {owner} pyproject.toml: {type(exc).__name__}"
        ) from exc
    project = document.get("project")
    if not isinstance(project, dict):
        raise RuntimePluginProjectError(
            f"{owner} pyproject.toml must define a PEP 621 [project] table"
        )
    return project


def _require_matching_identity(
    project: dict[str, object],
    expected_name: str,
    expected_version: str,
    path: Path,
    *,
    canonical_extension: bool,
) -> None:
    """Keep package metadata tied to the folder-discovered plugin identity."""

    name = project.get("name")
    if not isinstance(name, str) or not name.strip():
        raise RuntimePluginProjectError(f"[project].name must be a string: {path}")
    manifest = _require_matching_project_name(
        name, expected_name, canonical_extension=canonical_extension
    )
    version = project.get("version")
    if not isinstance(version, str) or not version.strip():
        raise RuntimePluginProjectError(f"[project].version must be a string: {path}")
    try:
        matches = Version(version) == Version(expected_version)
    except InvalidVersion as exc:
        raise RuntimePluginProjectError(
            f"runtime plugin {expected_name!r} pyproject version must be PEP 440"
        ) from exc
    if not matches:
        raise RuntimePluginProjectError(
            f"runtime plugin {expected_name!r} pyproject version {version!r} must "
            f"match {manifest} metadata.version {expected_version!r}"
        )


def _require_matching_project_name(
    name: str, expected_name: str, *, canonical_extension: bool
) -> str:
    """Bind a distribution name to its manifest while retaining legacy errors."""

    if not canonical_extension:
        if canonicalize_name(name) != canonicalize_name(expected_name):
            raise RuntimePluginProjectError(
                f"runtime plugin {expected_name!r} pyproject name {name!r} must "
                "match plugin.yaml metadata.name"
            )
        return "plugin.yaml"
    expected_distribution = f"harnest-extension-{expected_name}"
    if canonicalize_name(name) != canonicalize_name(expected_distribution):
        raise RuntimePluginProjectError(
            f"runtime plugin {expected_name!r} pyproject name {name!r} must be "
            f"{expected_distribution!r} for extension.yaml metadata.name"
        )
    return "extension.yaml"


def _project_dependencies(
    project: dict[str, object], path: Path
) -> tuple[str, ...]:
    """Validate statically discoverable dependencies and Python compatibility."""

    dynamic = project.get("dynamic", [])
    if not isinstance(dynamic, list) or not all(
        isinstance(item, str) for item in dynamic
    ):
        raise RuntimePluginProjectError(f"[project].dynamic must be a list: {path}")
    if "dependencies" in dynamic:
        raise RuntimePluginProjectError(
            f"runtime plugin dependencies must be static PEP 621 metadata: {path}"
        )
    requires_python = project.get("requires-python")
    if requires_python is not None:
        if not isinstance(requires_python, str):
            raise RuntimePluginProjectError(
                f"[project].requires-python must be a string: {path}"
            )
        try:
            SpecifierSet(requires_python)
        except InvalidSpecifier as exc:
            raise RuntimePluginProjectError(
                f"invalid [project].requires-python in {path}"
            ) from exc
    return tuple(requirement for _name, requirement in _dependency_items(project, path))


def _dependency_items(
    project: dict[str, object], path: Path
) -> tuple[tuple[str, str], ...]:
    """Canonicalize PEP 508 requirements for deterministic merge behavior."""

    dependencies = project.get("dependencies", [])
    if not isinstance(dependencies, list) or not all(
        isinstance(item, str) for item in dependencies
    ):
        raise RuntimePluginProjectError(f"[project].dependencies must be a list: {path}")
    return _requirement_items(tuple(dependencies))


def _requirement_items(
    dependencies: Sequence[str],
) -> tuple[tuple[str, str], ...]:
    """Parse and order requirements without retaining credential-bearing URLs."""

    items: list[tuple[str, str]] = []
    for value in dependencies:
        try:
            requirement = Requirement(value)
        except InvalidRequirement as exc:
            raise RuntimePluginProjectError(
                "runtime dependency must be a valid PEP 508 requirement"
            ) from exc
        items.append((canonicalize_name(requirement.name), str(requirement)))
    return tuple(sorted(items))


def _merge_declarations(
    declarations: Sequence[tuple[str, str, str]],
) -> tuple[ResolvedDependency, ...]:
    """Retain every constraint so the environment resolver can solve them jointly."""

    merged: dict[str, tuple[list[str], list[str]]] = {}
    for name, requirement, owner in declarations:
        current = merged.get(name)
        if current is None:
            merged[name] = ([requirement], [owner])
            continue
        # Compatible ranges are deliberately not guessed here. The same uv
        # invocation that installs the environment is the authoritative PEP
        # 508 solver and produces better conflict diagnostics than a partial
        # compiler implementation could.
        if requirement not in current[0]:
            current[0].append(requirement)
        if owner not in current[1]:
            current[1].append(owner)
    return tuple(
        ResolvedDependency(name, " && ".join(requirements), tuple(owners))
        for name, (requirements, owners) in sorted(merged.items())
    )


__all__ = [
    "ResolvedDependency",
    "RuntimePluginProject",
    "RuntimePluginProjectError",
    "load_runtime_plugin_project",
    "resolve_runtime_plugin_dependencies",
]
