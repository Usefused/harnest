"""Private, non-destructive capability archives with collision-safe recovery."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import uuid

from fastapi import HTTPException

from .files import source_path
from .ownership_paths import directory_path, fingerprint, relocate, validate_directory


def _directory(root: Path, *, create: bool = False) -> Path:
    """Reject linked archive ancestors before reading or writing project-local recovery data."""
    path = root
    for part in (".harnest", "builder-deleted"):
        path = path / part
        if path.is_symlink() or path.exists() and not path.is_dir():
            raise HTTPException(422, "Deleted capability storage must use regular project directories.")
        if create:
            path.mkdir(exist_ok=True, mode=0o700)
    return path


def _entry(root: Path, identity: str) -> Path:
    """Check recovery identity at the storage boundary, including direct internal callers."""
    if len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
        raise HTTPException(422, "Invalid deleted capability identity.")
    path = _directory(root) / identity
    if path.is_symlink() or not path.is_dir():
        raise HTTPException(404, "Deleted capability not found.")
    return path


def _metadata(entry: Path) -> dict:
    """Read bounded archive metadata without following a replaced manifest or content link."""
    path = entry / "metadata.json"
    if path.is_symlink() or (entry / "source").is_symlink():
        raise HTTPException(422, "Linked recovery files are excluded.")
    source = entry / "source"
    if source.exists() and not (stat.S_ISREG(source.stat().st_mode) or source.is_dir()):
        raise HTTPException(422, "Special recovery files are excluded.")
    try:
        with path.open("rb") as stream:
            data = stream.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("Oversized metadata")
        value = json.loads(data)
        if not isinstance(value, dict) or not isinstance(value.get("path"), str):
            raise ValueError("Invalid metadata")
        return value
    except (ValueError, OSError):
        raise HTTPException(422, "Cannot read deleted capability metadata.") from None


def archive(root: Path, preview: dict) -> dict:
    """Publish recovery metadata before an atomic source move so interruptions remain recoverable."""
    relative = preview["path"]
    path = root / relative
    source = directory_path(root, relative) if path.is_dir() else source_path(root, relative)
    entry = _directory(root, create=True) / uuid.uuid4().hex
    entry.mkdir(mode=0o700)
    value = {"identity": entry.name, "path": relative, "deleted_at": datetime.now(timezone.utc).isoformat(),
             "files": preview["files"], "directory": source.is_dir()}
    metadata = entry / "metadata.json"
    try:
        with metadata.open("x") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        # Both locations belong to this project, preserving permissions and binary assets.
        source.rename(entry / "source")
    except OSError:
        metadata.unlink(missing_ok=True)
        entry.rmdir()
        raise
    return value


def listing(root: Path) -> list[dict]:
    """Bound filesystem discovery and omit completed restores and interrupted pre-move records."""
    directory = _directory(root)
    if not directory.exists():
        return []
    items = []
    for index, item in enumerate(directory.iterdir()):
        if index >= 3000:
            raise HTTPException(413, "Recovery history exceeds 3,000 entries.")
        entry = _entry(root, item.name)
        source = entry / "source"
        # A crash before the source move and a completed restore both leave no
        # archived source. Neither should prevent access to other recoverable items.
        if not source.exists() and not source.is_symlink():
            continue
        value = _metadata(entry)
        items.append({key: value[key] for key in ("identity", "path", "deleted_at", "files")})
    return sorted(items, key=lambda item: item["deleted_at"], reverse=True)


def restore(root: Path, identity: str) -> dict:
    """Recover a complete archived capability without overwriting new source at its original path."""
    entry = _entry(root, identity)
    value = _metadata(entry)
    source = entry / "source"
    if not source.exists():
        raise HTTPException(409, "This capability has already been restored.")
    destination = directory_path(root, value["path"]) if source.is_dir() else source_path(root, value["path"])
    if destination.exists() or destination.is_symlink():
        raise HTTPException(409, "The original path is occupied. Rename the new capability before restoring.")
    if source.is_dir():
        validate_directory(source)
        fingerprint(source)  # Enforce the same bounded package contract used for deletion.
    destination.parent.mkdir(parents=True, exist_ok=True)
    relocate(source, destination)
    return {"path": value["path"], "files": value["files"], "identity": identity}
