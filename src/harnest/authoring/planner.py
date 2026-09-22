"""Compose Harnest migrations and company pack edits into one reviewable snapshot."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ValidationError

from ..upgrade import apply_upgrade, plan_upgrade
from .context import LOCK, ProjectContext, ProjectFiles, ProjectYAML
from .contracts import ChangeKind, ChangePlan, ProjectChange, ProjectError, ProjectPack, ProjectPlan, _File
from .filesystem import project_root, snapshot, write_snapshot
from .operations import OperationEngine, read_lock


def _typed_options(pack: ProjectPack, values: Mapping[str, Any] | None, initializing: bool) -> BaseModel | None:
    """Validate inputs before invoking trusted hooks without storing them in the lock."""
    if pack.options is None:
        if values:
            raise ProjectError(f"{pack.name}: pack declares no options")
        return None
    if values is None and not initializing:
        return None
    unknown = set(values or {}) - set(pack.options.model_fields)
    if unknown:
        raise ProjectError(f"{pack.name}: unknown options: {', '.join(sorted(unknown))}")
    return _validate_model(pack.options, values or {}, pack.name)


def _validate_model(model: type[BaseModel], values: Any, name: str) -> BaseModel:
    """Report field locations/types without printing potentially sensitive input values."""
    try:
        return model.model_validate(values)
    except ValidationError as exc:
        fields = [f"{'.'.join(map(str, e['loc']))}: {e['type']}" for e in exc.errors(include_input=False)]
        raise ProjectError(f"{name}: validation failed ({'; '.join(fields)})") from exc


def _change_kind(before: _File | None, after: _File | None) -> ChangeKind:
    """Classify one final net effect, collapsing intermediate migration writes."""
    if before is None:
        return ChangeKind.CREATE
    return ChangeKind.DELETE if after is None else ChangeKind.UPDATE


def _changes(before: Mapping[str, _File], after: Mapping[str, _File], owners: dict[str, set[str]]) -> tuple[ProjectChange, ...]:
    """Produce stable, content-free summaries for the final combined filesystem diff."""
    return tuple(ProjectChange(_change_kind(before.get(path), after.get(path)), path,
                               tuple(sorted(owners.get(path, {'harnest'}))))
                 for path in sorted(set(before) | set(after)) if before.get(path) != after.get(path))


def _run_core_init(stage: Path, command: Sequence[str], framework: str, minimal: bool) -> dict[str, _File]:
    """Use the shipped CLI scaffold as the single source of Harnest init behavior."""
    arguments = [*command, 'init', str(stage), '--framework', framework]
    if minimal:
        arguments.append('--minimal')
    result = subprocess.run(arguments, capture_output=True, text=True, check=False)
    if result.returncode:
        raise ProjectError(f"Harnest init failed (exit {result.returncode}): {result.stderr.strip()}")
    return snapshot(stage)


def _run_core_upgrade(stage: Path, before: dict[str, _File]) -> tuple[dict[str, _File], list[str]]:
    """Run the existing migration engine only on the disposable project snapshot."""
    write_snapshot(stage, before)
    core = plan_upgrade(stage)
    if core.blockers:
        return dict(before), list(core.blockers)
    apply_upgrade(core)
    return snapshot(stage), []


def _pack_hooks(pack: ProjectPack, lock: dict[str, Any], initializing: bool) -> list[Any]:
    """Require a complete consecutive migration chain and refuse implicit pack adoption."""
    if initializing:
        return [pack._initializer] if pack._initializer else []
    version = lock['packs'].get(pack.name)
    if version is None:
        raise ProjectError(f"{pack.name}: project has no pack version; explicit adoption is required")
    if version > pack.schema_version:
        raise ProjectError(f"{pack.name}: project schema is newer than the installed pack")
    missing = [v for v in range(version, pack.schema_version) if v not in pack._migrations]
    if missing:
        raise ProjectError(f"{pack.name}: missing migrations from versions {missing}")
    return [pack._migrations[v] for v in range(version, pack.schema_version)]


def _run_pack(pack: ProjectPack, engine: OperationEngine, name: str,
              values: Mapping[str, Any] | None, initializing: bool) -> None:
    """Run hooks in schema order so each sees earlier staged changes."""
    options = _typed_options(pack, values, initializing)
    hooks = _pack_hooks(pack, engine.lock, initializing)
    for hook in hooks:
        context = ProjectContext(name, options, ProjectFiles(engine.files, pack.templates), ProjectYAML(engine.files))
        proposals = _invoke_hook(pack, hook, context)
        if not isinstance(proposals, ChangePlan):
            raise ProjectError(f"{pack.name}: hook must return ChangePlan")
        for operation in proposals.operations:
            engine.apply(pack.name, operation)
    if pack.config_model is not None:
        data = ProjectYAML(engine.files).read(pack.config_file)
        _validate_model(pack.config_model, data, pack.config_file)
    engine.lock['packs'][pack.name] = pack.schema_version


def _invoke_hook(pack: ProjectPack, hook: Any, context: ProjectContext) -> ChangePlan:
    """Turn failed trusted hooks into blocked plans without leaking callback inputs."""
    try:
        return hook(context)
    except ProjectError:
        raise
    except Exception as exc:
        raise ProjectError(f"{pack.name}: hook failed ({type(exc).__name__})") from exc


def _finish_lock(engine: OperationEngine) -> None:
    """Store only versions and ownership hashes; option values stay out of the lock."""
    engine.lock['claims'].sort(key=lambda c: (c['path'], c['key'], c['owner']))
    encoded = (json.dumps(engine.lock, sort_keys=True, indent=2) + '\n').encode()
    engine.files[LOCK] = _File(encoded)
    engine.owners[LOCK] = {'harnest'}


def _validate_tree(files: dict[str, _File]) -> None:
    """Detect impossible file/directory layouts while still inside the planner."""
    for name in files:
        for parent in Path(name).parents:
            if parent.as_posix() in files:
                raise ProjectError(f"{name}: parent is a file")


def _structural_conflicts(before: dict[str, _File], after: dict[str, _File]) -> list[str]:
    """Require explicit handling for file/directory conversions, not unsafe write ordering."""
    old_parents = {p.as_posix() for name in before for p in Path(name).parents}
    new_parents = {p.as_posix() for name in after for p in Path(name).parents}
    conflicts = (set(after) & old_parents) | (set(before) & new_parents)
    return [f"{path}: file/directory conversion requires manual migration" for path in sorted(conflicts)]


def _core_conflicts(engine: OperationEngine, paths: list[str]) -> list[str]:
    """Require review if core rewrites overlap company-owned files or fields."""
    blockers = []
    for path in paths:
        try:
            engine._claim_check('harnest', path, [])
        except ProjectError as exc:
            blockers.append(str(exc))
    return blockers


class ProjectPlanner:
    """Plan init and upgrade using explicitly supplied trusted project packs."""
    def __init__(self, packs: Sequence[ProjectPack], *, harnest_command: Sequence[str] = ('harnest',)) -> None:
        """Keep pack ordering explicit and never import packages named by project files."""
        self.packs = tuple(packs)
        names = [pack.name for pack in packs]
        if len(names) != len(set(names)):
            raise ProjectError("duplicate project pack names")
        if not harnest_command:
            raise ProjectError("harnest_command cannot be empty")
        self.harnest_command = tuple(harnest_command)

    def plan_init(self, directory: str | Path, *, options: Mapping[str, Mapping[str, Any]] | None = None,
                  framework: str = 'adk', minimal: bool = False) -> ProjectPlan:
        """Scaffold and customise a disposable project; leave the requested target untouched."""
        root = project_root(directory)
        if root.exists() and any(root.iterdir()):
            raise ProjectError("init requires an absent or empty directory")
        return self._plan(root, options or {}, initializing=True, framework=framework, minimal=minimal)

    def plan_upgrade(self, directory: str | Path, *, options: Mapping[str, Mapping[str, Any]] | None = None) -> ProjectPlan:
        """Compose core migration and pack migration proposals without live writes."""
        root = project_root(directory)
        if not root.is_dir():
            raise ProjectError("upgrade requires an existing project")
        return self._plan(root, options or {}, initializing=False)

    def _plan(self, root: Path, options: Mapping[str, Mapping[str, Any]], *, initializing: bool,
              framework: str = 'adk', minimal: bool = False) -> ProjectPlan:
        """Capture immutable before/after snapshots and make all failures apply blockers."""
        before = snapshot(root)
        existed = root.exists()
        with tempfile.TemporaryDirectory(prefix='harnest-project-plan-') as temporary:
            stage = Path(temporary) / root.name
            if initializing:
                after, blockers = _run_core_init(stage, self.harnest_command, framework, minimal), []
            else:
                after, blockers = _run_core_upgrade(stage, before)
        engine = OperationEngine(after, read_lock(before))
        core_paths = [change.path for change in _changes(before, after, {})]
        if not initializing:
            blockers.extend(_core_conflicts(engine, core_paths))
        blockers.extend(self._compose(engine, root.name, options, initializing, bool(blockers)))
        blockers.extend(_structural_conflicts(before, engine.files))
        return ProjectPlan(root, _changes(before, engine.files, engine.owners), tuple(blockers),
                           MappingProxyType(before), MappingProxyType(dict(engine.files)), existed)

    def _compose(self, engine: OperationEngine, name: str, options: Mapping[str, Mapping[str, Any]],
                 initializing: bool, blocked: bool) -> list[str]:
        """Stop on a failed prerequisite; partial staged changes are never applicable."""
        if blocked:
            return []
        available = {pack.name for pack in self.packs}
        unknown = (set(engine.lock['packs']) | set(options)) - available
        if unknown:
            return [f"missing installed project packs: {', '.join(sorted(unknown))}"]
        try:
            for pack in self.packs:
                _run_pack(pack, engine, name, options.get(pack.name), initializing)
            _validate_tree(engine.files)
            _finish_lock(engine)
        except (ProjectError, ValueError, OSError) as exc:
            return [str(exc)]
        return []
