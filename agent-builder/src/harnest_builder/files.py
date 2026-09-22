"""Bounded workspace access and revision-checked, source-preserving edits."""

from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from threading import RLock

from fastapi import HTTPException
import yaml

try:
    import tomllib
except ImportError:
    import tomli as tomllib

LIMIT = 1024 * 1024
SUFFIXES = {".py", ".yaml", ".yml", ".json", ".md", ".mdx", ".toml", ".txt", ".lock", ".csv", ".j2", ".html", ".css", ".js"}
SKIP = {"node_modules", "__pycache__", "dist", "build", "venv"}
NAME = re.compile(r"[a-z][a-z0-9_-]{0,62}\Z")
PROJECT_SCAN_LIMIT = 10000


def name(value: str) -> str:
    """Constrain project and capability identities to a single non-option segment."""
    if not NAME.fullmatch(value):
        raise HTTPException(422, "Use a lowercase name starting with a letter, then letters, digits, - or _ (up to 63 characters).")
    return value


def source_path(root: Path, relative: str) -> Path:
    """Reject traversal and every linked ancestor, including absent-file destinations."""
    parts = PurePosixPath(relative)
    if not canonical_source(relative, parts):
        raise HTTPException(422, "Use a relative source path.")
    if any(p.startswith(".") or p in SKIP for p in parts.parts):
        raise HTTPException(422, "Hidden, generated, and environment files are excluded.")
    target = root / relative
    if target.suffix not in SUFFIXES or not target.resolve().is_relative_to(root):
        raise HTTPException(422, "Choose a text source file inside this project.")
    if linked_path(root, parts):
        raise HTTPException(422, "Linked source files and folders are excluded.")
    return target


def canonical_source(relative: str, parts: PurePosixPath) -> bool:
    """Give each on-disk file one API identity so alias paths cannot bypass duplicate checks."""
    return bool(relative) and not parts.is_absolute() and "\\" not in relative and relative == parts.as_posix()


def linked_path(root: Path, parts: PurePosixPath) -> bool:
    """Check every lexical ancestor rather than only the final resolved destination."""
    return any(root.joinpath(*parts.parts[:i]).is_symlink() for i in range(1, len(parts.parts) + 1))


def validate(path: Path, text: str) -> None:
    """Validate text formats without importing or running authored Python."""
    if len(text.encode()) > LIMIT:
        raise HTTPException(413, "Source files are limited to 1 MiB.")
    if path.name == "harnest-deployment.yaml":
        validate_deployment(text)
        return
    parsers = {".py": ast.parse, ".json": json.loads, ".yaml": lambda value: list(yaml.safe_load_all(value)), ".yml": lambda value: list(yaml.safe_load_all(value)), ".toml": tomllib.loads}
    try:
        parsers.get(path.suffix, str)(text)
    except (ValueError, SyntaxError, yaml.YAMLError) as error:
        raise HTTPException(422, f"Invalid {path.name}: {error}") from error


def validate_deployment(text: str) -> None:
    """Explain the manifest boundary before model or editor changes reach disk."""
    from harnest.provisioner_config import parse_manifest, ProvisionError

    try:
        documents = list(yaml.safe_load_all(text))
        if len(documents) != 1 or isinstance(documents[0], dict) and "apiVersion" in documents[0]:
            raise HTTPException(422, "harnest-deployment.yaml requires one Harnest deployment document (version, name, backend, agents, services). Save Kubernetes Deployment/Service documents to deploy/kubernetes.yaml instead, or use Deploy → Configure from project.")
        parse_manifest(text)
    except (yaml.YAMLError, ProvisionError) as error:
        raise HTTPException(422, "Invalid harnest-deployment.yaml: " + str(error)) from error


def read(root: Path, relative: str) -> dict:
    """Use content digests so browser saves cannot silently overwrite external edits."""
    path = source_path(root, relative)
    if not path.exists():
        return {"path": relative, "text": "", "revision": ""}
    if not path.is_file():
        raise HTTPException(422, "The source path is not a file.")
    with path.open("rb") as stream:
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise HTTPException(413, "Source files are limited to 1 MiB.")
    try:
        return {"path": relative, "text": data.decode("utf-8"), "revision": hashlib.sha256(data).hexdigest()}
    except UnicodeDecodeError as error:
        raise HTTPException(422, "The source file is not UTF-8 text.") from error


def replace(path: Path, text: str) -> None:
    """Atomically publish complete text and preserve existing file permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".builder-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o600)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def inventory(root: Path) -> list[str]:
    """Prune build state before walking and fail explicitly rather than omit source silently."""
    result = []
    for directory, folders, filenames in os.walk(root, followlinks=False):
        folders[:] = sorted(p for p in folders if not p.startswith(".") and p not in SKIP and not (Path(directory) / p).is_symlink())
        for filename in sorted(filenames):
            path = Path(directory) / filename
            if editable_file(path):
                result.append(path.relative_to(root).as_posix())
        if len(result) > 3000:
            raise HTTPException(413, "This project exceeds the builder's 3,000 source file limit.")
    return sorted(result)


def editable_file(path: Path) -> bool:
    """Exclude hidden and binary files without excluding Python package initializers."""
    return not path.name.startswith(".") and path.suffix in SUFFIXES and not path.is_symlink()


def project_root(path: Path) -> bool:
    """Reject linked projects and linked ancestors, including replacements after discovery."""
    config = path / "config.yaml"
    return path.resolve() == path and config.is_file() and not config.is_symlink()


def discovery_error(error: OSError) -> None:
    """Surface incomplete discovery instead of presenting a silently partial workspace."""
    raise HTTPException(422, "Cannot read the workspace tree. Check folder permissions.") from error


def discover_projects(root: Path) -> list[Path]:
    """Find agent boundaries in a bounded walk without traversing generated or linked trees."""
    result = []
    for count, (directory, folders, _) in enumerate(os.walk(root, followlinks=False, onerror=discovery_error)):
        if count >= PROJECT_SCAN_LIMIT:
            raise HTTPException(413, "Workspace discovery exceeds 10,000 folders. Choose a more specific workspace.")
        path = Path(directory)
        # Virtual environments can have arbitrary names; their marker is authoritative.
        if (path / "pyvenv.cfg").is_file():
            folders[:] = []
        elif project_root(path):
            result.append(path)
            # An agent owns its descendants: fixtures and subagents are not projects.
            folders[:] = []
        else:
            folders[:] = sorted(p for p in folders if not p.startswith(".") and p not in SKIP and not (path / p).is_symlink())
    return result


class Workspace:
    """Own the launch workspace and explicitly opened project roots; serialize mutations."""

    def __init__(self, root: Path):
        """Keep the launch root fixed while registering additional selected folders separately."""
        self.root = root.resolve()
        self.lock = RLock()
        self.registered = {}
        self.discovered = {}

    def project(self, identity: str, *, existing: bool = True) -> Path:
        """Resolve a stable project identity and reject replaced or linked project roots."""
        selected = self.registered.get(identity, self.discovered.get(identity))
        if selected is not None:
            path = selected
            if path.is_symlink() or path.resolve() != path:
                raise HTTPException(422, "The selected project location changed. Open it again.")
        else:
            path = self.root if identity == "." else self.root / name(identity)
        if path.is_symlink() or (selected is None and not path.resolve().is_relative_to(self.root)):
            raise HTTPException(422, "Linked projects are excluded.")
        if existing and not source_path(path, "config.yaml").is_file():
            raise HTTPException(404, "Choose a Harnest project containing config.yaml.")
        return path

    def register(self, root: Path) -> str:
        """Keep stable identities so open jobs and proposals cannot target a different folder."""
        identity = self._identity(root)
        if identity.startswith("@"):
            self.registered[identity] = root
        return identity

    def _identity(self, root: Path) -> str:
        """Preserve existing root/child IDs and give nested or external paths stable IDs."""
        if root == self.root:
            return "."
        if root.parent == self.root and NAME.fullmatch(root.name):
            return root.name
        return "@" + hashlib.sha256(str(root).encode()).hexdigest()[:24]

    def projects(self) -> list[dict]:
        """Refresh recursive discovery while retaining only explicitly opened extra projects."""
        discovered = {self._identity(p): p for p in discover_projects(self.root)}
        # Publish only a completed scan; failed refreshes leave active project IDs intact.
        self.discovered = discovered
        candidates = discovered | self.registered
        return [self._description(key, path) for key, path in sorted(candidates.items(), key=lambda item: str(item[1])) if project_root(path)]

    def _description(self, identity: str, path: Path) -> dict:
        """Keep project names compatible while exposing unambiguous picker labels."""
        label = str(path)
        if path.is_relative_to(self.root):
            label = path.relative_to(self.root).as_posix() if path != self.root else path.name
        return {"id": identity, "name": path.name, "label": label, "path": str(path)}

    def apply(self, identity: str, changes: list[dict]) -> list[dict]:
        """Preflight the complete change set and restore prior text if an I/O write fails."""
        with self.lock:
            root = self.project(identity)
            before = self._preflight(root, changes)
            written = []
            try:
                for change in changes:
                    replace(source_path(root, change["path"]), change["text"])
                    written.append(change["path"])
            except OSError:
                self._restore(root, before, written)
                raise
            return [read(root, change["path"]) for change in changes]

    def _preflight(self, root: Path, changes: list[dict]) -> dict:
        """Check all revisions and formats before committing any member of a proposal."""
        before = {}
        for change in changes:
            relative = change["path"]
            if relative in before:
                raise HTTPException(422, "A change set cannot contain duplicate paths.")
            current = read(root, relative)
            if current["revision"] != change["revision"]:
                raise HTTPException(409, f"{relative} changed on disk. Reload and review it before saving.")
            validate(source_path(root, relative), change["text"])
            before[relative] = current
        return before

    def _restore(self, root: Path, before: dict, written: list[str]) -> None:
        """Roll back only files this transaction actually replaced or created."""
        for relative in reversed(written):
            original = before[relative]
            path = source_path(root, relative)
            if original["revision"]:
                replace(path, original["text"])
            else:
                path.unlink(missing_ok=True)
