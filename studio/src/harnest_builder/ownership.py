"""Source-backed capability ownership for managed, folder-composed agents."""

from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .files import inventory, read, source_path
from .ownership_paths import directory_path, fingerprint, resolve_moves, relocate

ANCHOR = re.compile(r"(?:subagents/[a-z][a-z0-9_]*/)*subagents/([a-z][a-z0-9_]*)(/agent)?\.py\Z")
KINDS = {"tools": "tool", "mcp": "mcp"}


class Assignment(BaseModel):
    """Move one reviewed capability to one concrete managed agent scope."""
    model_config = ConfigDict(extra="forbid", strict=True)
    project: str
    resource: str = ""
    resources: list[str] = Field(default_factory=list, max_length=100)
    owner: str
    revision: str


def _agent_call(text: str, export: str) -> ast.Call | None:
    """Recognize static managed exports without executing user-authored modules."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == export for t in statement.targets):
            continue
        value = statement.value
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "Agent":
            return value
    return None


def _owner(root: Path, path: str) -> dict | None:
    """Describe flat and nested exports using Harnest's filename identity contract."""
    match = ANCHOR.fullmatch(path)
    if path != "agent.py" and match is None:
        return None
    export = "root_agent" if path == "agent.py" else match[1]
    if _agent_call(read(root, path)["text"], export) is None:
        return None
    flat = path != "agent.py" and not path.endswith("/agent.py")
    folder = path.removesuffix(".py") if flat else str(PurePosixPath(path).parent)
    return {"id": path, "path": path, "name": "Root agent" if path == "agent.py" else export, "folder": "" if folder == "." else folder + "/", "flat": flat}


def _resources(owner: dict, files: list[str]) -> list[dict]:
    """Expose only directly discovered Python tools and MCP declarations in this scope."""
    if owner["flat"]:
        return []
    found = []
    for directory, kind in KINDS.items():
        prefix = owner["folder"] + directory + "/"
        for path in files:
            if not path.startswith(prefix):
                continue
            name = path[len(prefix):]
            if "/" not in name and name.endswith(".py") and not name.startswith("_"):
                found.append({"path": path, "owner": owner["id"], "kind": kind, "name": name[:-3]})
    return found + _skills(owner, files)


def _skills(owner: dict, files: list[str]) -> list[dict]:
    """Discover complete skill directories through their required entrypoint."""
    found = []
    prefix = owner["folder"] + "skills/"
    for path in files:
        if path.startswith(prefix) and path.endswith("/SKILL.md"):
            relative = path[len(prefix):]
            if relative.count("/") == 1:
                found.append({"path": path.removesuffix("/SKILL.md"), "owner": owner["id"], "kind": "skill", "name": relative.split("/")[0], "directory": "skills"})
    return found


def _subagent_resources(agents: list[dict]) -> list[dict]:
    """Treat subagent exports as movable scopes while retaining their entire descendants."""
    result = []
    for agent in agents:
        if agent["id"] == "agent.py":
            continue
        parent = max((a for a in agents if a["id"] != agent["id"] and agent["path"].startswith(a["folder"] + "subagents/")), key=lambda a: len(a["folder"]))
        path = agent["path"] if agent["flat"] else agent["folder"].rstrip("/")
        result.append({"path": path, "anchor": agent["path"], "owner": parent["id"], "kind": "subagent", "name": agent["name"], "directory": "subagents"})
    return result


def _revision(root: Path, files: list[str], agents: list[dict], resources: list[dict]) -> str:
    """Include complete skill/subagent packages in the optimistic ownership snapshot."""
    paths = {"config.yaml", *(a["path"] for a in agents)}
    revisions = [(path, read(root, path)["revision"]) for path in sorted(paths)]
    for resource in resources:
        path = resource["path"]
        value = fingerprint(directory_path(root, path)) if (root / path).is_dir() else read(root, path)["revision"]
        revisions.append((path, value))
    return hashlib.sha256(json.dumps([files, revisions]).encode()).hexdigest()


def describe(root: Path, files: list[str], config: dict) -> dict:
    """Return revision-bound ownership, keeping dynamic and advanced composition in source."""
    if config.get("mode") != "managed" or _owner(root, "agent.py") is None:
        return {"available": False, "agents": [], "resources": [], "reason": "Capability reassignment requires a managed Agent root. Edit explicit or dynamic wiring in source."}
    agents = [owner for path in files if (owner := _owner(root, path)) is not None]
    resources = [resource for owner in agents for resource in _resources(owner, files)]
    resources.extend(_subagent_resources(agents))
    revision = _revision(root, files, agents, resources)
    return {"available": True, "agents": agents, "resources": resources, "revision": revision}


def _relocatable(root: Path, path: str) -> None:
    """Reject location-dependent Python rather than silently breaking relative imports or assets."""
    try:
        tree = ast.parse(read(root, path)["text"])
    except SyntaxError as error:
        raise HTTPException(422, f"Fix the Python syntax in {path} before moving it.") from error
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            raise HTTPException(422, f"{path} uses relative imports. Update these in source before moving it.")
        if isinstance(node, ast.Name) and node.id == "__file__":
            raise HTTPException(422, f"{path} uses its file location. Update its paths in source before moving it.")


def _requested(body: Assignment) -> list[str]:
    """Accept one resource or a category batch without ambiguous duplicate identities."""
    paths = body.resources or [body.resource]
    if body.resources and body.resource or len(paths) != len(set(paths)):
        raise HTTPException(422, "Choose each capability only once.")
    return paths


def _invalid_resource(path: str) -> str:
    """Explain native ownership restrictions instead of accepting decorative connections."""
    policies = {"extensions": "Extensions are application-wide and must remain on the root agent.", "plugins": "Plugins are root-only in Harnest; move a standalone skill or MCP connection instead.", "lifecycle": "Lifecycle hooks belong to the application, not individual subagents.", "tasks": "Durable tasks belong to the application and cannot be assigned through an agent edge.", "cron": "Schedules target durable tasks, not agents.", "sandbox": "Sandbox access requires an explicit Agent.sandboxes grant in source; moving a folder does not grant access.", "models": "Shared data models belong to the root application.", "lib": "Shared libraries belong to the root application."}
    for part in PurePosixPath(path).parts:
        if part in policies:
            return policies[part]
    return "This connection is structural or code-owned. Tools, MCP connections, skills, and managed subagents can be reassigned."


def _selected(snapshot: dict, paths: list[str]) -> list[dict]:
    """Collapse a category to whole capabilities, avoiding duplicate moves of nested scopes."""
    resources = {r["path"]: r for r in snapshot["resources"]}
    for path in paths:
        if path not in resources:
            raise HTTPException(422, _invalid_resource(path))
    return [resources[path] for path in paths if not any(path.startswith(parent + "/") for parent in paths if parent != path)]


def _validate_destination(resources: list[dict], target: dict) -> None:
    """Reject cycles and redundant ownership before converting any destination agent."""
    for resource in resources:
        path = resource["path"]
        if target["id"] == path or target["id"].startswith(path + "/"):
            raise HTTPException(422, "An agent cannot own itself or one of its ancestors.")
        if resource.get("anchor") == target["id"]:
            raise HTTPException(422, "An agent cannot own itself.")
        if resource["owner"] == target["id"]:
            raise HTTPException(409, "This capability already belongs to that agent.")


def _resource_move(root: Path, resource: dict, target: dict) -> tuple[str, str]:
    """Move whole scopes, keeping package-relative imports and assets together."""
    path = resource["path"]
    if not (root / path).is_dir():
        _relocatable(root, path)
    directory = resource.get("directory") or next(key for key, kind in KINDS.items() if kind == resource["kind"])
    return path, target["folder"] + directory + "/" + PurePosixPath(path).name


def _plan(root: Path, snapshot: dict, body: Assignment) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """Validate a complete category or individual capability move against native agent scopes."""
    agents = {a["id"]: a for a in snapshot["agents"]}
    if body.owner not in agents:
        raise HTTPException(422, "Drop the connection onto a managed agent. Capabilities cannot own other capabilities.")
    target = agents[body.owner]
    resources = _selected(snapshot, _requested(body))
    _validate_destination(resources, target)
    moves, additions = _conversion(root, target) if target["flat"] else ([], {})
    moves.extend(_resource_move(root, resource, target) for resource in resources)
    destinations = [destination for _, destination in moves]
    if len(set(destinations)) != len(destinations):
        raise HTTPException(409, "These capabilities have the same destination name. Rename them before reconnecting.")
    _preflight(root, moves, additions)
    return moves, additions


def _conversion(root: Path, target: dict) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """Give a converted flat export the instructions file required by Harnest folder scopes."""
    _relocatable(root, target["path"])
    folder = root / target["folder"]
    if folder.exists() or folder.is_symlink():
        raise HTTPException(409, "The destination subagent folder already exists. Resolve it in source first.")
    call = _agent_call(read(root, target["path"])["text"], target["name"])
    text = _instruction(call)
    return [(target["path"], target["folder"] + "agent.py")], {target["folder"] + "instructions.md": text}


def _instruction(call: ast.Call) -> str:
    """Mirror literal prompts while retaining explicit Python expressions as the authority."""
    instruction = next((k.value for k in call.keywords if k.arg == "instruction"), None)
    if instruction is None or isinstance(instruction, ast.Constant) and instruction.value is None:
        raise HTTPException(422, "Set the flat subagent's instruction in source before assigning capabilities.")
    if isinstance(instruction, ast.Constant) and isinstance(instruction.value, str) and instruction.value.strip():
        return instruction.value.strip()
    return "Instructions are configured explicitly in agent.py."


def _move_file(source: Path, destination: Path) -> None:
    """Publish without overwriting an existing path and retain the original if unlink fails."""
    relocate(source, destination)


def _parents(path: Path, root: Path, created: list[Path]) -> None:
    """Record directories introduced by this transaction so failed conversions leave no scopes."""
    missing = []
    while path != root and not path.exists():
        missing.append(path)
        path = path.parent
    for directory in reversed(missing):
        directory.mkdir()
        created.append(directory)


def _preflight(root: Path, moves: list[tuple[str, str]], additions: dict[str, str]) -> tuple[list, list]:
    """Resolve all paths and collisions before creating any destination scope."""
    pairs = resolve_moves(root, moves)
    added = [(source_path(root, path), text) for path, text in additions.items()]
    if any(path.exists() for path, _ in added):
        raise HTTPException(409, "A destination instruction file already exists. Refresh before reconnecting.")
    return pairs, added


def apply(root: Path, moves: list[tuple[str, str]], additions: dict[str, str]) -> None:
    """Preflight every destination, then roll back completed moves after a filesystem failure."""
    pairs, added = _preflight(root, moves, additions)
    written, created, published = [], [], []
    try:
        for source, destination in pairs:
            _parents(destination.parent, root, created)
            _move_file(source, destination)
            written.append((source, destination))
        for path, text in added:
            _parents(path.parent, root, created)
            _create_file(path, text)
            published.append(path)
    except OSError:
        for path in published:
            path.unlink()
        for source, destination in reversed(written):
            _move_file(destination, source)
        for directory in reversed(created):
            directory.rmdir()
        raise


def _create_file(path: Path, text: str) -> None:
    """Write new required metadata exclusively and remove partial content on failure."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text + "\n")
    except OSError:
        path.unlink()
        raise


def _prepare(workspace, body, configuration):
    """Re-read the reviewed source state for both preview and final publication."""
    root = workspace.project(body.project)
    snapshot = describe(root, inventory(root), configuration(root))
    if not snapshot["available"]:
        raise HTTPException(422, snapshot["reason"])
    if snapshot["revision"] != body.revision:
        raise HTTPException(409, "Agent ownership or source changed on disk. Refresh before reconnecting.")
    moves, additions = _plan(root, snapshot, body)
    return root, moves, additions


def _result(moves, additions):
    """Describe file and complete-directory changes without exposing source content."""
    return {"moves": [{"from": old, "to": new} for old, new in moves], "created": list(additions)}


def install_routes(app, workspace, jobs, configuration) -> None:
    """Serialize capability moves against commands and all other editor writes."""
    @app.post("/api/ownership/preview")
    def preview(body: Assignment):
        """Validate a dropped edge with the same rules as save, without changing any files."""
        with workspace.lock, jobs.lock:
            jobs.ensure_idle()
            _, moves, additions = _prepare(workspace, body, configuration)
            return _result(moves, additions)

    @app.put("/api/ownership")
    def assign(body: Assignment):
        """Check the entire reviewed ownership snapshot before changing source locations."""
        with workspace.lock, jobs.lock:
            jobs.ensure_idle()
            root, moves, additions = _prepare(workspace, body, configuration)
            apply(root, moves, additions)
            return _result(moves, additions)
