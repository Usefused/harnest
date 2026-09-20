"""Bounded filesystem snapshots and complete-package moves for ownership editing."""

import hashlib
import os
from pathlib import Path
import stat

from fastapi import HTTPException

from .files import source_path


def directory_path(root: Path, relative: str) -> Path:
    """Apply source containment and linked-ancestor checks to a directory identity."""
    return source_path(root, relative + "/agent.py").parent


def entries(path: Path):
    """Walk complete packages, including supporting assets, without traversing symlinks."""
    count = 0
    for parent, directories, files in os.walk(path, followlinks=False):
        for name in sorted(directories + files):
            count += 1
            if count > 3000:
                raise HTTPException(413, "This package exceeds the 3,000-entry move limit.")
            yield Path(parent) / name


def fingerprint(path: Path) -> str:
    """Detect edits to binary, hidden, and supporting package members before a move."""
    digest, total = hashlib.sha256(), 0
    for item in entries(path):
        metadata = item.lstat()
        digest.update(str((item.relative_to(path).as_posix(), metadata.st_mode)).encode())
        if stat.S_ISREG(metadata.st_mode):
            total += metadata.st_size
            if total > 128 * 1024 * 1024:
                raise HTTPException(413, "This package exceeds the 128 MiB move limit.")
            with item.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
        else:
            digest.update(str(metadata.st_mtime_ns).encode())
    return digest.hexdigest()


def validate_directory(path: Path) -> None:
    """Never transfer linked, special, or environment directories as hidden move side effects."""
    for item in entries(path):
        mode = item.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise HTTPException(422, "This package contains a linked or special file. Remove it before reconnecting.")
        if item.name in {".git", ".venv", "venv", "node_modules"}:
            raise HTTPException(422, "This package contains an environment or repository directory. Move its source separately.")


def resolve_moves(root: Path, moves: list[tuple[str, str]]) -> list[tuple[Path, Path]]:
    """Resolve typed file/package moves and refuse occupied destination scopes."""
    pairs = []
    for old, new in moves:
        directory = (root / old).is_dir()
        resolve = directory_path if directory else source_path
        source, destination = resolve(root, old), resolve(root, new)
        if not source.exists() or destination.exists():
            raise HTTPException(409, "A source moved or the destination already exists. Refresh before reconnecting.")
        if directory:
            validate_directory(source)
        pairs.append((source, destination))
    return pairs


def relocate(source: Path, destination: Path) -> None:
    """Move complete directory trees or publish files without replacing another file."""
    if source.is_dir():
        # Destinations were preflighted under the workspace lock; a directory move
        # preserves binary resources, empty directories, permissions, and relative paths.
        if destination.exists():
            raise FileExistsError(destination)
        source.rename(destination)
        return
    os.link(source, destination, follow_symlinks=False)
    try:
        source.unlink()
    except OSError:
        destination.unlink()
        raise
