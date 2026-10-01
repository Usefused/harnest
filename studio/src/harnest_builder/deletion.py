"""Reviewable capability deletion with project-local recovery and source conflict checks."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
import re

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import deleted_store
from .files import inventory, read, source_path
from .ownership_paths import directory_path, entries, fingerprint, validate_directory

CAPABILITY_DIRECTORIES = {"tools", "subagents", "mcp", "skills", "plugins", "extensions", "sandbox",
                          "channels", "tasks", "cron", "lifecycle", "models", "lib", "evals", "tests"}
PACKAGE_ANCHORS = {"skills": "SKILL.md", "plugins": "plugin.json", "extensions": "extension.yaml", "subagents": "agent.py"}


class Selection(BaseModel):
    """Bind deletion to one source capability and the exact preview the user reviewed."""
    model_config = ConfigDict(extra="forbid", strict=True)
    project: str
    path: str = Field(min_length=1, max_length=1024)
    revision: str = Field(default="", max_length=64)


class Recovery(BaseModel):
    """Identify a project-owned archived capability without accepting arbitrary backup paths."""
    model_config = ConfigDict(extra="forbid", strict=True)
    project: str
    identity: str = Field(pattern=r"^[0-9a-f]{32}$")


def target(root, relative: str):
    """Resolve a capability, expanding package entrypoints without accepting core files or categories."""
    relative = relative.rstrip("/")
    parts = PurePosixPath(relative).parts
    if len(parts) < 2 or parts[0] not in CAPABILITY_DIRECTORIES:
        raise HTTPException(422, "Choose a capability, not a core project file or category.")
    # Deleting an entrypoint removes its complete package, including supporting assets.
    if len(parts) >= 3 and PACKAGE_ANCHORS.get(parts[-3]) == parts[-1]:
        relative = str(PurePosixPath(relative).parent)
    if PurePosixPath(relative).name in CAPABILITY_DIRECTORIES:
        raise HTTPException(422, "Choose an individual capability within this category.")
    path = root / relative
    resolve = directory_path if path.is_dir() else source_path
    path = resolve(root, relative)
    if not path.exists():
        raise HTTPException(409, "This capability no longer exists. Refresh the project.")
    if path.is_dir():
        validate_directory(path)
    return relative, path


def _members(root, path) -> list[str]:
    """Include binary and supporting package members in the deletion preview."""
    if path.is_file():
        return [path.relative_to(root).as_posix()]
    return sorted(item.relative_to(root).as_posix() for item in entries(path) if item.is_file())


def _references(root, relative: str, members: list[str]) -> list[dict]:
    """Surface possible explicit references without rewriting dynamic user-authored wiring."""
    name = PurePosixPath(relative).stem
    pattern = re.compile(r"(?<![\w])" + re.escape(name) + r"(?![\w])")
    found, total = [], 0
    for path in inventory(root):
        if path in members or not path.endswith((".py", ".yaml", ".yml", ".json", ".toml")):
            continue
        document = read(root, path)
        total += len(document["text"].encode())
        if total > 16 * 1024 * 1024:
            raise HTTPException(413, "Reference preview exceeds 16 MiB of source. Remove the capability through source control.")
        if pattern.search(document["text"]):
            found.append({"path": path, "revision": document["revision"]})
    return found


def plan(root, relative: str) -> dict:
    """Capture the complete source and possible references so stale previews cannot delete newer work."""
    relative, path = target(root, relative)
    files = _members(root, path)
    content = fingerprint(path) if path.is_dir() else read(root, relative)["revision"]
    references = _references(root, relative, files)
    revision = hashlib.sha256(json.dumps([relative, content, references]).encode()).hexdigest()
    return {"path": relative, "files": files, "revision": revision,
            "references": [item["path"] for item in references]}


def install_routes(app, workspace, jobs) -> None:
    """Serialize archive and recovery against editor writes, builds, and live preview processes."""
    @app.post("/api/capabilities/delete-preview")
    def preview(body: Selection):
        """Preview one capability without moving files or creating an archive."""
        with workspace.lock:
            return plan(workspace.project(body.project), body.path)

    @app.post("/api/capabilities/delete")
    def remove(body: Selection):
        """Archive the complete reviewed capability; never purge source or runtime data."""
        with workspace.lock, jobs.lock:
            jobs.ensure_idle()
            jobs.ensure_idle(serving=True)
            root = workspace.project(body.project)
            current = plan(root, body.path)
            if not body.revision or current["revision"] != body.revision:
                raise HTTPException(409, "Capability or references changed. Preview the deletion again.")
            return deleted_store.archive(root, current)

    @app.get("/api/capabilities/deleted")
    def deleted(project: str):
        """List recoverable capabilities for the selected project without exposing their source."""
        with workspace.lock:
            return {"items": deleted_store.listing(workspace.project(project))}

    @app.post("/api/capabilities/restore")
    def restore(body: Recovery):
        """Restore original source only when every destination remains unoccupied."""
        with workspace.lock, jobs.lock:
            jobs.ensure_idle()
            jobs.ensure_idle(serving=True)
            return deleted_store.restore(workspace.project(body.project), body.identity)
