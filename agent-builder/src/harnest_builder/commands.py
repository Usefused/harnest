"""Translate explicit UI actions into the public Harnest CLI contract."""

from __future__ import annotations

from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .catalog import COMMANDS, resource_name
from pathlib import Path, PurePosixPath

from .files import linked_path, name, read
from .folders import directory


class Command(BaseModel):
    """The browser selects operations, never a shell command or executable."""
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["init", "add", "compile", "test", "smoke", "eval", "sync", "serve", "run", "install-extension", "search-extensions", "provision"]
    operation: Literal["init", "plan", "apply", "status", "stop", "remove", "history", "rollback"] = "plan"
    revision: int | None = Field(default=None, ge=1)
    manifest_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    environment: str = Field(default="local", pattern=r"^[a-z][a-z0-9-]{0,19}$")
    project: str = "."
    directory: str = Field(default="", max_length=4096)
    name: str = ""
    kind: str = ""
    framework: str = "adk"
    mode: str = "managed"
    profile: str = "minimal"
    url: str = Field(default="", max_length=8192)
    token_env: str = Field(default="", max_length=128)
    port: int = Field(default=1907, ge=1024, le=65535)
    input: str = Field(default="", max_length=32000)


def arguments(workspace, command: Command) -> tuple[list[str], str]:
    """Resolve registered projects or an explicitly selected initialization destination."""
    if command.action == "init":
        return _initialize(workspace, command)
    project = workspace.project(command.project)
    root = str(project)
    if linked_path(project, PurePosixPath(".harnest/builder")):
        raise HTTPException(422, "Build output cannot use linked directories.")
    if command.action == "add":
        return _add(root, command), command.project
    simple = {
        "provision": _provision_arguments(root, command),
        "compile": ["compile", root, "--output", root + "/.harnest/builder"],
        "test": ["test", root],
        "smoke": ["test", root, "--smoke"],
        "eval": ["test", root, "--evals"],
        "sync": ["env", "sync", root],
        "serve": ["serve", root, "--reload", "--host", "127.0.0.1", "--port", str(command.port)],
        "run": ["run", "--", root, command.input],
    }
    if command.action in simple:
        return simple[command.action], command.project
    if command.action == "install-extension":
        source = str(directory(command.name)) if command.name.startswith(("/", "~")) else name(command.name)
        return ["extensions", "install", source, "--project", root], command.project
    if command.action == "search-extensions":
        return ["extensions", "search", "--json", "--limit", "20", "--", command.input], command.project
    raise HTTPException(422, "Unsupported Harnest command.")


def _initialize(workspace, command: Command) -> tuple[list[str], str]:
    """Let harnest init own the complete scaffold and refuse replacing an existing directory."""
    name(command.name)
    if "_" in command.name:
        raise HTTPException(422, "Project names use hyphens, not underscores.")
    parent = directory(command.directory) if command.directory else workspace.root
    root = parent / command.name
    if root.exists():
        raise HTTPException(409, "That project directory already exists; open it instead.")
    if command.framework not in {"adk", "langgraph"} or command.mode not in {"managed", "advanced"}:
        raise HTTPException(422, "Choose ADK or LangGraph and managed or advanced mode.")
    if command.profile not in {"minimal", "guided", "example"}:
        raise HTTPException(422, "Choose a supported scaffold profile.")
    args = ["init", str(root), "--framework", command.framework, "--mode", command.mode]
    if command.profile != "guided":
        args.append("--" + command.profile)
    return args, workspace.register(root)


def _add(root: str, command: Command) -> list[str]:
    """Preserve the CLI's managed-mode and framework validation for generated resources."""
    if command.kind not in COMMANDS:
        raise HTTPException(422, "This component uses source authoring, not harnest add.")
    identity = resource_name(command.name)
    if command.kind == "extension":
        return ["extensions", "init", identity, "--project", root]
    args = ["add", command.kind, identity, "--project", root]
    if command.kind == "mcp":
        args.extend(["--url", command.url])
        if command.token_env:
            args.extend(["--token-env", command.token_env])
    return args


def _provision_arguments(root: str, command: Command) -> list[str]:
    """Reject stale manifest reviews and bind snapshot revisions only to rollback or its preview."""

    args = ["provision", command.operation, "--project", root, "--environment", command.environment]
    if command.action != "provision":
        return args
    if command.operation == "apply" and command.manifest_revision is not None:
        if read(Path(root), "harnest-deployment.yaml")["revision"] != command.manifest_revision:
            raise HTTPException(409, "Deployment configuration changed. Refresh the deployment review before deploying.")
    if command.operation == "rollback" and command.revision is None:
        raise HTTPException(422, "Choose a successful deployment revision from history.")
    if command.revision is not None:
        if command.operation not in {"plan", "rollback"}:
            raise HTTPException(422, "A revision applies only to rollback or its preview.")
        args.extend(["--revision", str(command.revision)])
    return args
