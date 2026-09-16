"""Revision-checked local edits to the CLI-owned authoring workspace."""

from __future__ import annotations

import ast
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import yaml
try:
    import tomllib
except ImportError:  # Python 3.10 uses the same TOML parser through the backport.
    import tomli as tomllib
from threading import RLock
from typing import Any

from fastapi import HTTPException

from .logging import get_logger

from .playground_studio import _SOURCE_LIMIT, _SOURCE_SUFFIXES

_authoring_root: ContextVar[Path | None] = ContextVar("playground_authoring_root", default=None)
_AUDIT = get_logger("playground.authoring.audit")
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")


@contextmanager
def authoring_workspace(root: Path | None):
    """Lend a source directory only while the development app is constructed."""

    token = _authoring_root.set(root.resolve() if root else None)
    try:
        yield
    finally:
        _authoring_root.reset(token)


def configured_workspace() -> Path | None:
    """Capture the supervisor-owned workspace without trusting browser paths."""

    return _authoring_root.get()


@contextmanager
def authoring_audit(operation: str):
    """Correlate committed source changes with failures without recording authored content."""

    try:
        yield
    except Exception:
        _AUDIT.warning("authoring.failed", operation=operation, trigger="user", outcome="failed")
        raise
    else:
        _AUDIT.info("authoring.committed", operation=operation, trigger="user", outcome="committed")


def require_name(value: str) -> str:
    """Restrict generated resource filenames and Python identities to one segment."""

    if not _NAME.fullmatch(value):
        raise HTTPException(422, "Use a name starting with a letter, followed by letters, digits, or underscores")
    return value


class AuthoringStore:
    """Own bounded source writes and reject stale revisions before atomic replacement."""

    def __init__(self, root: Path, studio: Any):
        """Bind edits to a known source root and the served projection's ownership scopes."""

        self.root = root.resolve()
        self.studio = studio
        self._lock = RLock()
        self.connection_scopes = sorted({str(PurePosixPath(item["path"]).parent) for item in studio.projection["blocks"] if item["kind"] == "agent" and PurePosixPath(item["path"]).name == "agent.py"})
        self.paths = {item["path"] for item in studio.projection["files"]}
        self.scopes = sorted({str(PurePosixPath(item["path"]).parent) for item in studio.projection["blocks"] if item["kind"] in {"agent", "graph"}})

    def path(self, relative: str) -> Path:
        """Reject hidden files, traversal, and linked parents for both reads and creates."""

        parts = PurePosixPath(relative)
        if parts.is_absolute() or not parts.parts or any(part.startswith((".", "_")) for part in parts.parts):
            raise HTTPException(404, "Source file not available")
        candidate = self.root / relative
        if candidate.suffix not in _SOURCE_SUFFIXES or not self._contained_path(candidate):
            raise HTTPException(404, "Source file not available")
        if relative not in self.paths and not self._generated_path(parts):
            raise HTTPException(404, "Source file not available")
        return candidate

    def _contained_path(self, path: Path) -> bool:
        """Validate parents without requiring a new source file to exist yet."""

        if not path.resolve().is_relative_to(self.root):
            return False
        parts = path.relative_to(self.root).parts
        return not any(self.root.joinpath(*parts[:index]).is_symlink() for index in range(1, len(parts) + 1))

    def _generated_path(self, path: PurePosixPath) -> bool:
        """Allow only conventional MCP and eval documents in existing agent scopes."""

        scope = str(path.parent.parent)
        if scope not in self.scopes:
            return False
        if path.parent.name == "mcp":
            return path.suffix == ".py" and bool(_NAME.fullmatch(path.stem))
        return path.parent.name == "evals" and (path.name.endswith(".evalset.json") or path.name == "test_config.json")

    def read(self, relative: str) -> dict[str, str]:
        """Read bounded current source, using an empty revision for an absent document."""

        path = self.path(relative)
        if not path.exists():
            return {"path": relative, "text": "", "revision": ""}
        with path.open("rb") as stream:
            contents = stream.read(_SOURCE_LIMIT + 1)
        if len(contents) > _SOURCE_LIMIT:
            raise HTTPException(413, "Source exceeds 1 MiB")
        return {"path": relative, "text": contents.decode("utf-8"), "revision": hashlib.sha256(contents).hexdigest()}

    def save(self, relative: str, text: str, revision: str) -> dict[str, str]:
        """Validate before writing; the supervisor compiles before replacing the live build."""

        if len(text.encode()) > _SOURCE_LIMIT:
            raise HTTPException(413, "Source exceeds 1 MiB")
        path = self.path(relative)
        validate_document(path, text)
        with self._lock:
            self._check_revision(relative, revision)
            self._replace(path, text)
            self.paths.add(relative)
            return self.read(relative)

    def _check_revision(self, relative: str, revision: str) -> None:
        """Never overwrite changes made by another browser or editor since the read."""

        if self.read(relative)["revision"] != revision:
            raise HTTPException(409, "This file changed. Reopen it to review the latest version before saving")

    def _replace(self, path: Path, text: str) -> None:
        """Hide partial writes from the reload watcher and preserve existing permissions."""

        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".harnest-edit-", dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o600)
            self.path(path.relative_to(self.root).as_posix())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def remove_mcp(self, relative: str, revision: str) -> None:
        """Remove one agent-owned factory, never a shared remote server or another scope."""

        path = self.path(relative)
        if path.parent.name != "mcp" or path.suffix != ".py":
            raise HTTPException(422, "Only MCP connection files can be removed here")
        with self._lock:
            self._check_revision(relative, revision)
            path.unlink()

    def catalog(self) -> dict[str, Any]:
        """Describe editable suites and agent ownership without disclosing source roots."""

        suites = []
        for scope in self.scopes:
            folder = self.root / scope / "evals"
            if folder.is_dir() and not folder.is_symlink():
                for path in sorted(folder.glob("*.evalset.json")):
                    relative = path.relative_to(self.root).as_posix()
                    document = self.read(relative)
                    suites.append({**document, "scope": scope})
        return {"available": True, "scopes": self.scopes, "connectionScopes": self.connection_scopes, "suites": suites, "files": sorted(self.paths), "reload": True}


def validate_document(path: Path, text: str) -> None:
    """Validate native formats without executing authored Python during a save."""

    try:
        if path.suffix == ".py":
            ast.parse(text)
        elif path.suffix == ".json":
            _validate_json(path, text)
        elif path.suffix in {".yaml", ".yml"}:
            yaml.safe_load(text)
        elif path.suffix == ".toml":
            tomllib.loads(text)
    except (ValueError, SyntaxError, yaml.YAMLError) as exc:
        raise HTTPException(422, f"Invalid {path.name}: {exc}") from exc


def _validate_json(path: Path, text: str) -> None:
    """Reuse ADK's authoring schema, preserving advanced fields and rejecting duplicate IDs."""

    from .bundle import _reject_duplicate_json_keys

    payload = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    if path.name.endswith(".evalset.json"):
        from google.adk.evaluation.eval_set import EvalSet
        suite = EvalSet.model_validate(payload)
        if suite.eval_set_id != path.name.removesuffix(".evalset.json"):
            raise ValueError("eval_set_id must match the filename")
        ids = [case.eval_id for case in suite.eval_cases]
        if len(ids) != len(set(ids)):
            raise ValueError("Case IDs must be unique within the suite")
    elif path.name == "test_config.json":
        from google.adk.evaluation.eval_config import EvalConfig
        EvalConfig.model_validate(payload)


def mcp_source(name: str, transport: str, endpoint: str, arguments: list[str], tools: list[str] | None, token_env: str) -> str:
    """Generate one managed factory using environment references rather than stored tokens."""

    require_name(name)
    keywords = f", tools={tools!r}"
    if transport == "stdio":
        call = f"MCPClient.stdio({endpoint!r}, *{arguments!r}{keywords})"
    else:
        from urllib.parse import urlsplit
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise HTTPException(422, "Enter an HTTP(S) URL without embedded credentials")
        headers = f", headers={{'Authorization': 'Bearer ' + os.environ[{require_name(token_env)!r}]}}" if token_env else ""
        call = f"MCPClient.streamable_http({endpoint!r}{keywords}{headers})"
    return f'import os\nfrom harnest.mcp import MCPClient\n\ndef client():\n    """Connect this agent to {name}."""\n    return {call}\n'
