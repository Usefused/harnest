"""Plan framework API and manifest migrations in the existing backup transaction."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from typing import Any

import yaml

from .source_tree import IGNORED_SOURCE_DIRECTORIES
from .upgrade import UpgradeAction, UpgradeError, _record_composed_rewrite
from .upgrade_framework_names import CAPABILITY_RENAMES


_EXCLUDED = IGNORED_SOURCE_DIRECTORIES | {"node_modules", "build", "dist", "site-packages"}


@dataclass(frozen=True)
class _Source:
    path: Path
    content: str
    pending: tuple[int, UpgradeAction] | None
    module: str
    digest: str


def plan_framework_upgrade(
    root: Path, framework: str, actions: list[UpgradeAction], blockers: list[str]
) -> tuple[tuple[str, str], ...]:
    """Compose alias-aware source changes and capability renames before planned moves."""

    sources = _read_sources(root, actions, blockers)
    imported_types = _imported_types(sources, root, blockers)
    for source in sources:
        _plan_file(root, source, framework, imported_types, actions, blockers)
    return tuple((source.path.relative_to(root).as_posix(), source.digest) for source in sources)


def _read_sources(
    root: Path, actions: list[UpgradeAction], blockers: list[str]
) -> list[_Source]:
    """Snapshot pending authored content once for indexing and composed rewrites."""

    sources: list[_Source] = []
    for path in _authored_files(root, blockers):
        if _removed_by_plan(root, path, actions):
            continue
        try:
            sources.append(_snapshot_source(root, path, actions))
        except (UpgradeError, OSError, UnicodeError) as error:
            blockers.append(str(error))
    return sources


def _snapshot_source(root: Path, path: Path, actions: list[UpgradeAction]) -> _Source:
    """Keep inference text and its pre-mutation digest tied to the same file read."""

    relative = path.relative_to(root)
    pending = next(((index, action) for index, action in enumerate(actions)
                    if action.kind == "rewrite" and action.path == relative.as_posix()), None)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    content = raw.decode("utf-8")
    if pending is not None:
        if pending[1].digest != digest:
            raise UpgradeError(f"upgrade source changed during planning: {relative}")
        content = pending[1].content if pending[1].content is not None else content
    module = ".".join(relative.with_suffix("").parts)
    if module.split(".")[0] in {"lib", "models"}:
        module = "harnest." + module
    return _Source(path, content, pending, module, digest)


def _imported_types(sources: list[_Source], root: Path, blockers: list[str]) -> dict[str, str]:
    """Resolve authored reexports without executing project code or guessing names."""

    imported: dict[str, str] = {}
    python_sources = [source for source in sources if source.path.suffix == ".py"]
    if _module_collisions(python_sources, root, blockers):
        return {}
    # Each pass can resolve another import edge. Bound cycles by the module
    # count and discard unstable inference rather than rewriting on a guess.
    for _ in range(len(python_sources) + 1):
        updated = _source_exports(python_sources, imported)
        if updated == imported:
            return updated
        imported = updated
    return {}


def _module_names(source: _Source) -> set[str]:
    """Treat package initializers and virtual namespace aliases as one module."""

    module = source.module.removesuffix(".__init__")
    return {module, module.removeprefix("harnest.")}


def _module_collisions(sources: list[_Source], root: Path, blockers: list[str]) -> bool:
    """Reject competing files instead of borrowing types from the wrong import target."""

    owners: dict[str, Path] = {}
    conflicts: set[tuple[str, str]] = set()
    for source in sources:
        for module in _module_names(source):
            previous = owners.setdefault(module, source.path)
            if previous != source.path:
                conflicts.add((previous.relative_to(root).as_posix(), source.path.relative_to(root).as_posix()))
    for first, second in sorted(conflicts):
        blockers.append(f"{first} and {second}: ambiguous module paths require manual framework migration")
    return bool(conflicts)


def _source_exports(sources: list[_Source], imported: dict[str, str]) -> dict[str, str]:
    """Index both Harnest's virtual library namespace and ordinary module imports."""

    from .upgrade_framework_source import framework_source_exports

    exports: dict[str, str] = {}
    for source in sources:
        try:
            names = framework_source_exports(
                source.content, imported_types=imported, module=source.module, path=str(source.path)
            )
        except UpgradeError:
            # The migration pass reports syntax errors once with the authored path.
            continue
        for prefix in _module_names(source):
            exports.update({f"{prefix}.{name}": owner for name, owner in names.items()})
    return exports


def _authored_files(root: Path, blockers: list[str]) -> Iterator[Path]:
    """Prune generated trees before walking and never follow project symlinks."""

    for path in authored_upgrade_files(root, blockers):
        if path.is_symlink():
            blockers.append(f"{path.relative_to(root)} is a symlink")
        else:
            yield path


def verify_framework_inventory(
    root: Path, actions: tuple[UpgradeAction, ...], expected: set[str]
) -> None:
    """Detect newly added import targets before a reviewed source plan is applied."""

    blockers: list[str] = []
    current = {path.relative_to(root).as_posix() for path in _authored_files(root, blockers)
               if not _removed_by_plan(root, path, actions)}
    if blockers or current != expected:
        raise UpgradeError("authored source inventory changed after planning; rerun the upgrade preview")


def authored_upgrade_files(root: Path, blockers: list[str] | None = None) -> Iterator[Path]:
    """Share the bounded authored-file inventory with every source migration pass."""

    diagnostics = blockers if blockers is not None else []
    for directory, folders, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        folders[:] = [name for name in sorted(folders) if _visit_folder(root, parent / name, diagnostics)]
        for name in sorted(files):
            path = parent / name
            if name.startswith(".") or not (name.endswith(".py") or name in {"extension.yaml", "plugin.yaml"}):
                continue
            yield path


def _visit_folder(root: Path, path: Path, blockers: list[str]) -> bool:
    """Ignore caches and reject authored links instead of reading another project."""

    if path.name in _EXCLUDED or path.name.startswith("."):
        return False
    if path.is_symlink():
        blockers.append(f"{path.relative_to(root)} is a symlink")
        return False
    return True


def _removed_by_plan(root: Path, path: Path, actions: Sequence[UpgradeAction]) -> bool:
    """Preserve deleted content and inactive framework files already made private."""

    relevant = (action for action in actions if action.path == path.relative_to(root).as_posix())
    return any(action.kind == "delete" or (
        action.kind == "move" and action.destination is not None
        and Path(action.destination).name.startswith("_")
    ) for action in relevant)


def _plan_file(
    root: Path, snapshot: _Source, framework: str, imported_types: dict[str, str],
    actions: list[UpgradeAction], blockers: list[str],
) -> None:
    """Record one digest-bound rewrite using the latest pending source."""

    from .upgrade_framework_source import migrate_framework_source

    path, source, pending = snapshot.path, snapshot.content, snapshot.pending
    relative = path.relative_to(root).as_posix()
    try:
        if path.suffix == ".py":
            updated = migrate_framework_source(
                source, framework=framework, path=relative,
                imported_types=imported_types, module=snapshot.module,
            )
        else:
            updated = _manifest_source(source, relative)
    except (UpgradeError, OSError, UnicodeError) as error:
        blockers.append(str(error))
        return
    if updated != source:
        _record_composed_rewrite(root, path, updated, pending, actions,
                                 "migrate framework APIs to compiler-selected adapters")


def _manifest_source(source: str, path: str) -> str:
    """Change only extension capability scalars, leaving ordinary YAML data intact."""

    try:
        document = yaml.compose(source)
        tokens = list(yaml.scan(source))
    except yaml.YAMLError:
        raise UpgradeError(f"{path}: cannot parse extension manifest") from None
    if not isinstance(document, yaml.MappingNode):
        raise UpgradeError(f"{path}: extension manifest must be a mapping")
    fields = _manifest_fields(document, path)
    kind = fields.get("kind")
    if not isinstance(kind, yaml.ScalarNode) or kind.value not in {"Extension", "RuntimePlugin"}:
        return source
    capabilities = fields.get("capabilities")
    if capabilities is None:
        return source
    names = _capability_items(capabilities, path)
    if not any(item.value in CAPABILITY_RENAMES for item in names):
        return source
    if any(isinstance(token, (yaml.AliasToken, yaml.AnchorToken)) for token in tokens):
        raise UpgradeError(f"{path}: review aliased extension capabilities manually")
    return _replace_capabilities(source, capabilities, names, tokens)


def _manifest_fields(document: yaml.MappingNode, path: str) -> dict[str, Any]:
    """Require unambiguous top-level keys before choosing a migration target."""

    fields: dict[str, Any] = {}
    for key, value in document.value:
        if not isinstance(key, yaml.ScalarNode) or key.value in fields:
            raise UpgradeError(f"{path}: extension manifest has ambiguous keys")
        fields[key.value] = value
    return fields


def _capability_items(node: yaml.Node, path: str) -> list[yaml.ScalarNode]:
    """Validate the literal capability list before merging renamed declarations."""

    if not isinstance(node, yaml.SequenceNode):
        raise UpgradeError(f"{path}: extension capabilities must be a list")
    if any(not isinstance(item, yaml.ScalarNode) or item.tag != "tag:yaml.org,2002:str"
           for item in node.value):
        raise UpgradeError(f"{path}: extension capabilities must be literal strings")
    names = [item.value for item in node.value]
    if len(names) != len(set(names)):
        raise UpgradeError(f"{path}: extension capabilities contain duplicates")
    return node.value


def _replace_capabilities(
    source: str, sequence: yaml.SequenceNode, items: list[yaml.ScalarNode], tokens: list[Any]
) -> str:
    """Preserve comments and scalar quoting while coalescing equivalent capabilities."""

    seen: set[str] = set()
    edits: list[tuple[int, int, str]] = []
    for item in items:
        updated = CAPABILITY_RENAMES.get(item.value, item.value)
        if updated in seen:
            edits.extend(_duplicate_item_edits(source, sequence, item, tokens))
        elif updated != item.value:
            quote = item.style if item.style in {"'", '"'} else ""
            edits.append((item.start_mark.index, item.end_mark.index, quote + updated + quote))
        seen.add(updated)
    for start, end, value in sorted(set(edits), reverse=True):
        source = source[:start] + value + source[end:]
    return source


def _duplicate_item_edits(
    source: str, sequence: yaml.SequenceNode, item: yaml.ScalarNode, tokens: list[Any]
) -> list[tuple[int, int, str]]:
    """Remove one obsolete list entry without removing adjacent user comments."""

    start, end = item.start_mark.index, item.end_mark.index
    if not sequence.flow_style:
        markers = [token for token in tokens if isinstance(token, yaml.BlockEntryToken)
                   and sequence.start_mark.index <= token.start_mark.index < start]
        marker = markers[-1]
        return [(marker.start_mark.index, marker.end_mark.index, ""), (start, end, "")]
    # Remove the preceding comma, including when several consecutive entries
    # collapse. Each duplicate owns a distinct separator; comments remain.
    commas = [token for token in tokens if isinstance(token, yaml.FlowEntryToken)
              and sequence.start_mark.index < token.start_mark.index < start]
    comma = commas[-1]
    return [(comma.start_mark.index, comma.end_mark.index, ""), (start, end, "")]
