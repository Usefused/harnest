"""Snapshot, stage and transactionally publish project plans."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import uuid
from typing import Iterator, Mapping

from .context import IGNORED
from .contracts import ProjectError, ProjectPlan, _File


def project_root(directory: str | Path) -> Path:
    """Reject a linked project root while allowing normal platform path aliases."""
    path = Path(directory).absolute()
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ProjectError("project must be a real directory, not a symlink")
    return path.resolve()


def snapshot(root: Path) -> dict[str, _File]:
    """Read source files without following links or copying runtime/cache directories."""
    result: dict[str, _File] = {}
    if not root.exists():
        return result
    project_root(root)
    for directory, folders, names in os.walk(root, followlinks=False, onerror=_walk_error):
        folders[:] = sorted(name for name in folders if name not in IGNORED)
        _reject_linked_folders(Path(directory), folders)
        for name in sorted(names):
            if name not in IGNORED:
                path = Path(directory) / name
                result[path.relative_to(root).as_posix()] = _read_file(path)
    return result


def _walk_error(error: OSError) -> None:
    """Do not silently plan against a partial snapshot of unreadable source."""
    raise ProjectError("cannot read the complete project source tree") from error


def _reject_linked_folders(directory: Path, folders: list[str]) -> None:
    """Refuse source aliases so a pack cannot affect files outside the project."""
    for name in folders:
        if (directory / name).is_symlink():
            raise ProjectError(f"source directory cannot be a symlink: {name}")


def _read_file(path: Path) -> _File:
    """Capture regular file contents and permissions for exact stale-plan checks."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ProjectError(f"source must be a regular file: {path.name}")
    return _File(path.read_bytes(), stat.S_IMODE(info.st_mode))


def write_snapshot(root: Path, files: Mapping[str, _File]) -> None:
    """Materialize an isolated snapshot for the existing Harnest migration engine."""
    root.mkdir(parents=True, exist_ok=True)
    for path, value in files.items():
        atomic_write(root / path, value)


def atomic_write(path: Path, value: _File) -> None:
    """Replace one file atomically while retaining its reviewed permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".harnest-project-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value.content)
        os.chmod(temporary, value.mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _backup(plan: ProjectPlan) -> Path:
    """Store recovery material privately and reject linked backup directories."""
    directory = plan.root
    for component in (".harnest", "project-backups"):
        directory /= component
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ProjectError("backup directory must be a real directory")
    backup = directory / uuid.uuid4().hex
    backup.mkdir(parents=True, mode=0o700)
    (backup / "plan.json").write_text(json.dumps(plan.public(), indent=2) + "\n")
    for change in plan.changes:
        if change.path in plan._before:
            atomic_write(backup / "source" / change.path, plan._before[change.path])
    return backup


def _missing_parents(path: Path, root: Path) -> list[Path]:
    """Remember only directories created by this apply for precise rollback."""
    missing = []
    while path != root and not path.exists():
        missing.append(path)
        path = path.parent
    return missing


def _publish(plan: ProjectPlan, created: set[Path]) -> None:
    """Apply reviewed files without replacing unrelated project directories."""
    for change in plan.changes:
        path = plan.root / change.path
        value = plan._after.get(change.path)
        if value is None:
            path.unlink()
        else:
            created.update(_missing_parents(path.parent, plan.root))
            atomic_write(path, value)


def _restore(plan: ProjectPlan, created: set[Path]) -> None:
    """Restore reviewed originals and remove only newly created empty directories."""
    for change in reversed(plan.changes):
        path = plan.root / change.path
        value = plan._before.get(change.path)
        if value is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, value)
    for path in sorted(created, key=lambda item: len(item.parts), reverse=True):
        path.rmdir()


def _verify_plan(plan: ProjectPlan) -> None:
    """Recheck the full source snapshot, including unrelated migration inputs."""
    if plan.blockers:
        raise ProjectError("project plan has blockers; resolve them and replan")
    if plan.root.exists() != plan._root_existed or snapshot(plan.root) != plan._before:
        raise ProjectError("project changed since planning; generate a fresh plan")


@contextmanager
def _apply_lock(root: Path) -> Iterator[None]:
    """Serialize cooperating publishers; a crash leaves an explicit recovery marker."""
    directory = root / '.harnest'
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ProjectError("backup directory must be a real directory")
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / 'project-apply.lock'
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ProjectError("another project apply is active; recover any interrupted apply before removing its lock") from exc
    try:
        os.close(fd)
        yield
    finally:
        path.unlink()


def _validate_targets(plan: ProjectPlan) -> None:
    """Reject structural replacements before source writes; preserve empty user folders."""
    for change in plan.changes:
        if change.path not in plan._after:
            continue
        target = plan.root / change.path
        if target.is_dir():
            raise ProjectError(f"{change.path}: file target is an existing directory")
        for parent in target.parents:
            if parent == plan.root:
                break
            if parent.exists() and not parent.is_dir():
                raise ProjectError(f"{change.path}: parent is an existing file")


def _apply_locked(plan: ProjectPlan) -> Path:
    """Back up and publish while the project is held against other pack applies."""
    if snapshot(plan.root) != plan._before:
        raise ProjectError("project changed since planning; generate a fresh plan")
    _validate_targets(plan)
    backup = _backup(plan)
    created: set[Path] = set()
    try:
        _publish(plan, created)
    except BaseException:
        _restore(plan, created)
        raise
    return backup


def apply_project_plan(plan: ProjectPlan) -> Path | None:
    """Reject stale inputs, back up originals, and roll back failed source writes."""
    _verify_plan(plan)
    if not plan.changes:
        return None
    # Exclusive creation prevents a failed init from cleaning up another writer's new project.
    plan.root.mkdir(parents=True, exist_ok=plan._root_existed)
    try:
        with _apply_lock(plan.root):
            return _apply_locked(plan)
    except BaseException:
        if not plan._root_existed:
            shutil.rmtree(plan.root)
        raise
