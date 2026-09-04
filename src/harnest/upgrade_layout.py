"""Backed-up, collision-checked migration to lifecycle and extension packages."""

from pathlib import Path
import symtable

import yaml

from .application_layout import ApplicationLayoutError, lifecycle_directory
from .runtime_plugins import discover_application_extensions, RuntimePluginConventionError
from .upgrade import UpgradeAction, _file_digest, _tree_digest, _rewrite


def plan_application_layout(root: Path, actions: list, blockers: list[str]) -> None:
    """Stage child renames before vacating the legacy root and relocating packages."""

    try:
        directory = lifecycle_directory(root)
        descriptors = discover_application_extensions(root)
    except (ApplicationLayoutError, RuntimePluginConventionError) as error:
        blockers.append(str(error))
        return
    legacy_root = root / "extensions"
    moving_root = directory == legacy_root
    # Guide-only legacy trees can move too, but never move canonical packages.
    if not (root / "lifecycle").exists() and legacy_root.exists():
        moving_root = moving_root or not any(
            item.directory.parent == legacy_root for item in descriptors
        )
    if moving_root:
        _move(root, legacy_root, root / "lifecycle", actions, blockers,
              kind="relocate_lifecycle")
    for descriptor in descriptors:
        if descriptor.entrypoint == "plugin:plugin":
            _plan_package(root, descriptor, actions, blockers, moving_root)


def _plan_package(root: Path, descriptor, actions: list, blockers: list[str], moving_root: bool) -> None:
    """Rename one explicitly declared legacy package, preserving its Python API."""

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
    actions.append(_rewrite(root, manifest, yaml.safe_dump(document, sort_keys=False),
                            "declare the canonical Harnest Extension manifest"))
    _move(root, manifest, directory / "extension.yaml", actions, blockers)
    _plan_entrypoint(root, directory, actions, blockers)
    hooks = directory / "extensions"
    if hooks.exists():
        _move(root, hooks, directory / "lifecycle", actions, blockers)
    # The old root must be vacated before it becomes the package container.
    actions.append(UpgradeAction(
        "relocate_extension", str(directory.relative_to(root)),
        "move the runtime package into extensions/ after lifecycle migration",
        destination=str(destination.relative_to(root)), digest=_tree_digest(directory),
    ))


def _plan_entrypoint(root: Path, directory: Path, actions: list, blockers: list[str]) -> None:
    """Add the new singleton export without rewriting authored Python identifiers."""

    source = directory / "plugin.py"
    text = source.read_text(encoding="utf-8")
    try:
        symbols = symtable.symtable(text, str(source), "exec")
    except SyntaxError:
        blockers.append(f"cannot migrate {source}: repair its Python syntax first")
        return
    # An existing binding could be business data; never overwrite it with our alias.
    if "extension" in symbols.get_identifiers() and symbols.lookup("extension").is_local():
        blockers.append(f"{source} already binds 'extension'; choose its canonical singleton manually")
        return
    replacement = text.rstrip() + "\n\n# Compatibility alias retains the existing singleton and callers.\nextension = plugin\n"
    actions.append(_rewrite(root, source, replacement, "export the existing singleton as extension"))
    _move(root, source, directory / "extension.py", actions, blockers)


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
