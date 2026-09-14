"""Backed-up, collision-checked migration to lifecycle and extension packages."""

import ast
from pathlib import Path
import re
import symtable

import yaml

from .application_layout import ApplicationLayoutError, lifecycle_directory
from .extension_descriptors import (
    ExtensionConventionError,
    discover_application_extensions,
    discover_legacy_extensions,
)
from .upgrade import UpgradeAction, _file_digest, _tree_digest, _rewrite


def plan_application_layout(root: Path, actions: list, blockers: list[str]) -> None:
    """Stage child renames before vacating the legacy root and relocating packages."""

    try:
        directory = lifecycle_directory(root)
        canonical = discover_application_extensions(root)
        legacy = discover_legacy_extensions(root / "plugins")
    except (ApplicationLayoutError, ExtensionConventionError) as error:
        blockers.append(str(error))
        return
    legacy_root = root / "extensions"
    moving_root = directory == legacy_root
    # Guide-only legacy trees can move too, but never move canonical packages.
    if not (root / "lifecycle").exists() and legacy_root.exists():
        moving_root = moving_root or not any(
            item.directory.parent == legacy_root for item in canonical
        )
    if moving_root:
        _move(root, legacy_root, root / "lifecycle", actions, blockers,
              kind="relocate_lifecycle")
    for descriptor in legacy:
        _plan_package(root, descriptor, actions, blockers, moving_root)


def _plan_package(root: Path, descriptor, actions: list, blockers: list[str], moving_root: bool) -> None:
    """Move one executable legacy package without touching Agent Plugins."""

    directory = descriptor.directory
    destination = root / "extensions" / descriptor.name
    if not moving_root and (destination.exists() or destination.is_symlink()):
        blockers.append(f"cannot migrate {directory}: destination {destination} exists")
        return
    manifest = directory / "plugin.yaml"
    document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    document["kind"] = "Extension"
    document["runtime"]["entrypoint"] = "extension:extension"
    if "requires" in document:
        document["requires"] = {"extensions": list(descriptor.requires)}
    contributions = _legacy_contributions(directory)
    if contributions:
        document["contributes"] = contributions
    actions.append(_rewrite(root, manifest, yaml.safe_dump(document, sort_keys=False),
                            "declare the canonical Harnest Extension manifest"))
    _move(root, manifest, directory / "extension.yaml", actions, blockers)
    _plan_entrypoint(root, directory, actions, blockers)
    _plan_extension_project(root, directory, descriptor.name, actions, blockers)
    hooks = directory / "extensions"
    if hooks.exists():
        _move(root, hooks, directory / "lifecycle", actions, blockers)
    # The old root must be vacated before it becomes the package container.
    actions.append(UpgradeAction(
        "relocate_extension", str(directory.relative_to(root)),
        "move the runtime package into extensions/ after lifecycle migration",
        destination=str(destination.relative_to(root)), digest=_tree_digest(directory),
    ))


def _legacy_contributions(directory: Path) -> dict[str, list[str]]:
    """Make formerly inferred Runtime Plugin content explicit after relocation."""

    result: dict[str, list[str]] = {}
    legacy_lifecycle = directory / "extensions"
    canonical_lifecycle = directory / "lifecycle"
    if legacy_lifecycle.is_dir() or canonical_lifecycle.is_dir():
        result["lifecycle"] = ["lifecycle/"]
    for kind in ("mcp", "skills", "subagents", "tools"):
        if (directory / kind).is_dir():
            result[kind] = [f"{kind}/"]
    return result


def _plan_entrypoint(root: Path, directory: Path, actions: list, blockers: list[str]) -> None:
    """Rename the unambiguous legacy singleton before moving its source file."""

    source = directory / "plugin.py"
    text = source.read_text(encoding="utf-8")
    try:
        symbols = symtable.symtable(text, str(source), "exec")
    except SyntaxError:
        blockers.append(f"cannot migrate {source}: repair its Python syntax first")
        return
    # An existing binding could be business data; never overwrite it with the
    # canonical singleton selected by the manifest.
    if "extension" in symbols.get_identifiers() and symbols.lookup("extension").is_local():
        blockers.append(f"{source} already binds 'extension'; choose its canonical singleton manually")
        return
    try:
        module = compile(text, str(source), "exec", ast.PyCF_ONLY_AST)
    except SyntaxError:
        blockers.append(f"cannot migrate {source}: repair its Python syntax first")
        return
    assignments = [
        node
        for node in module.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and _assignment_name(node) == "plugin"
    ]
    if len(assignments) != 1:
        blockers.append(f"{source} must bind exactly one top-level 'plugin' singleton")
        return
    target = assignments[0].targets[0] if isinstance(assignments[0], ast.Assign) else assignments[0].target
    start = _offset(text, target.lineno, target.col_offset)
    end = _offset(text, target.end_lineno, target.end_col_offset)
    replacement = text[:start] + "extension" + text[end:]
    actions.append(_rewrite(root, source, replacement, "rename the runtime singleton to extension"))
    _move(root, source, directory / "extension.py", actions, blockers)


def _plan_extension_project(
    root: Path,
    directory: Path,
    name: str,
    actions: list,
    blockers: list[str],
) -> None:
    """Prefix a legacy distribution name without reformatting its TOML."""

    path = directory / "pyproject.toml"
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or not path.is_file():
        blockers.append(f"{path} must be a regular file")
        return
    source = path.read_text(encoding="utf-8")
    replacement = _project_name_source(source, name)
    if replacement is None:
        blockers.append(
            f"{path} must declare one literal [project].name matching {name!r}"
        )
        return
    actions.append(
        _rewrite(
            root,
            path,
            replacement,
            f"rename the extension distribution to harnest-extension-{name}",
        )
    )


def _project_name_source(source: str, name: str) -> str | None:
    """Rewrite the literal project name only inside the PEP 621 table."""

    lines = source.splitlines(keepends=True)
    in_project = False
    matches: list[int] = []
    pattern = re.compile(r'^\s*name\s*=\s*(["\'])([^"\']+)\1\s*(?:#.*)?(?:\r?\n)?$')
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_project = stripped == "[project]"
            continue
        match = pattern.match(line)
        if in_project and match is not None and match.group(2) == name:
            matches.append(index)
    if len(matches) != 1:
        return None
    line = lines[matches[0]]
    indent = line[: len(line) - len(line.lstrip())]
    newline = "\n" if line.endswith("\n") else ""
    lines[matches[0]] = f'{indent}name = "harnest-extension-{name}"{newline}'
    return "".join(lines)


def _assignment_name(node: ast.Assign | ast.AnnAssign) -> str | None:
    """Return one simple assignment target without accepting unpacking."""

    if isinstance(node, ast.Assign):
        return node.targets[0].id if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) else None
    return node.target.id if isinstance(node.target, ast.Name) else None


def _offset(source: str, line: int, column: int) -> int:
    """Convert an AST source coordinate into a text offset."""

    return sum(len(value) for value in source.splitlines(keepends=True)[: line - 1]) + column


def _move(root: Path, source: Path, destination: Path, actions: list,
          blockers: list[str], *, kind: str = "move") -> None:
    """Reject collisions and links before recording a digest-bound move."""

    if destination.exists() or destination.is_symlink():
        blockers.append(f"cannot move {source}: destination {destination} exists")
        return
    if source.is_symlink() or any(item.is_symlink() for item in source.rglob("*")):
        blockers.append(f"cannot migrate symlinked tree: {source}")
        return
    digest = _tree_digest(source) if source.is_dir() else _file_digest(source)
    actions.append(UpgradeAction(kind, str(source.relative_to(root)),
                                 "adopt the lifecycle/ and extensions/ folder contract",
                                 destination=str(destination.relative_to(root)), digest=digest))
