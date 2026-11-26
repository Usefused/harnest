"""Read-only views and operation builders passed to trusted project pack hooks."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
from string import Template
from typing import Any, Mapping

import yaml
from pydantic import BaseModel

from .contracts import ChangePlan, ProjectError, WritePolicy, _File, _Operation


IGNORED = frozenset({".git", ".harnest", ".venv", "__pycache__", ".pytest_cache", ".cache"})
LOCK = "harnest-packs.lock"


def _is_ignored_template_entry(relative: Path, item: Path) -> bool:
    """Return True for VCS, cache, bytecode, or Finder files skipped by from_folder."""
    return any(part in IGNORED or part == ".DS_Store" for part in relative.parts) or item.suffix == ".pyc"


def relative_path(value: str, *, internal: bool = False) -> str:
    """Reject path aliases and reserved state before resolving a filesystem target."""
    path = PurePosixPath(value)
    if not value or not path.parts or path.is_absolute() or "\\" in value or str(path) != value:
        raise ProjectError(f"expected a canonical project-relative path: {value!r}")
    if any(part in {"..", ".", *IGNORED} for part in path.parts):
        raise ProjectError(f"reserved or escaping path: {value!r}")
    if not internal and value == LOCK:
        raise ProjectError(f"{LOCK} is owned by Harnest")
    return value


def yaml_mapping(content: bytes, path: str) -> dict[str, Any]:
    """Reject duplicate keys and multi-document configuration instead of losing data."""
    from ..server_config import _UniqueKeyLoader
    try:
        value = yaml.load(content, Loader=_UniqueKeyLoader)
    except (ValueError, yaml.YAMLError) as exc:
        raise ProjectError(f"invalid YAML mapping: {path}") from exc
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ProjectError(f"expected YAML mapping: {path}")
    return value


def key_path(value: tuple[str, ...]) -> tuple[str, ...]:
    """Require explicit nonempty string keys, never dotted-path guessing."""
    if not isinstance(value, tuple) or not value or any(not isinstance(k, str) or not k for k in value):
        raise ProjectError("YAML key must be a nonempty tuple of nonempty strings")
    return value


def policy_value(value: WritePolicy) -> WritePolicy:
    """Keep static authoring choices enum-based and reject typo-prone strings."""
    if not isinstance(value, WritePolicy):
        raise ProjectError("policy must be a WritePolicy enum")
    return value


class ProjectFiles:
    """Read snapshot files and declare text/template changes without writing them."""
    def __init__(self, files: Mapping[str, _File], templates: Path | None) -> None:
        """Keep file reads detached from the live project."""
        self._files, self._templates = files, templates

    def read_text(self, path: str) -> str:
        """Read an existing staged UTF-8 file."""
        try:
            return self._files[relative_path(path)].content.decode("utf-8")
        except KeyError as exc:
            raise ProjectError(f"file does not exist: {path}") from exc

    def write_text(self, path: str, content: str, *, policy: WritePolicy = WritePolicy.IF_MISSING) -> _Operation:
        """Propose text creation or a baseline-checked managed replacement."""
        return _Operation("write", relative_path(path), policy_value(policy), content.encode("utf-8"))

    def from_template(self, path: str, *, template: str, values: Mapping[str, str] | None = None,
                      policy: WritePolicy = WritePolicy.IF_MISSING) -> _Operation:
        """Render an inert UTF-8 dollar template using only explicit substitutions."""
        content = self._template_file(template).read_text(encoding="utf-8")
        if values is not None:
            content = Template(content).substitute(values)
        return self.write_text(path, content, policy=policy)

    def from_file(self, path: str, *, source: str,
                  policy: WritePolicy = WritePolicy.IF_MISSING) -> _Operation:
        """Copy a packaged document or binary asset unchanged through tracked file ownership."""
        return _Operation("write", relative_path(path), policy_value(policy), self._template_file(source).read_bytes())

    def from_folder(
        self,
        path: str,
        *,
        source: str | None = None,
        policy: WritePolicy = WritePolicy.IF_MISSING,
    ) -> ChangePlan:
        """Copy every regular file inside a packaged template folder recursively with tracked ownership."""
        destination_root = relative_path(path)
        # Default source to path so `from_folder("lib")` copies `templates/lib/` into `lib/`.
        effective_source = path if source is None else source
        source_root = self._template_directory(effective_source)
        resolved_policy = policy_value(policy)
        operations = self._collect_folder_operations(
            destination_root, source_root, effective_source, resolved_policy
        )
        return ChangePlan(*operations)

    def _template_file(self, source: str) -> Path:
        """Share one containment boundary for rendered templates and verbatim resources."""
        if self._templates is None:
            raise ProjectError("pack has no template directory")
        path = self._templates / relative_path(source)
        if not path.resolve().is_relative_to(self._templates.resolve()):
            raise ProjectError("template escapes its package directory")
        if not path.is_file():
            raise ProjectError(f"pack resource must be a regular file: {source}")
        return path

    def _template_directory(self, source: str) -> Path:
        """Resolve and validate a symlink-free folder inside the pack's template directory."""
        # Require an explicit templates root before reading packaged folders.
        if self._templates is None:
            raise ProjectError("pack has no template directory")
        path = self._templates / relative_path(source)
        # Reject symlinked folders and escaping paths before walking any children.
        if path.is_symlink() or not path.resolve().is_relative_to(self._templates.resolve()):
            raise ProjectError("template folder escapes its package directory")
        if not path.is_dir():
            raise ProjectError(f"pack resource must be a directory: {source}")
        return path

    def _collect_folder_operations(
        self,
        destination_root: str,
        source_root: Path,
        source_label: str,
        policy: WritePolicy,
    ) -> tuple[_Operation, ...]:
        """Walk a validated template folder deterministically and build tracked write operations."""
        operations: list[_Operation] = []
        for item in sorted(source_root.rglob("*"), key=lambda entry: entry.relative_to(source_root).as_posix()):
            relative = item.relative_to(source_root)
            # Skip VCS, cache, bytecode, and macOS Finder metadata files.
            if _is_ignored_template_entry(relative, item):
                continue
            # Refuse symlinks inside copied folders so pack templates never pull in outside files.
            if item.is_symlink():
                raise ProjectError(f"pack folder resource cannot be a symlink: {relative.as_posix()}")
            if item.is_dir():
                continue
            if not item.is_file():
                raise ProjectError(f"pack folder resource must be a regular file: {relative.as_posix()}")
            target = relative_path(f"{destination_root}/{relative.as_posix()}")
            operations.append(_Operation("write", target, policy, item.read_bytes()))
        # Fail clearly on empty folders rather than silently producing a no-op plan.
        if not operations:
            raise ProjectError(f"pack folder contains no files: {source_label}")
        return tuple(operations)

    def delete(self, path: str) -> _Operation:
        """Propose removal only if the pack still owns unchanged generated content."""
        return _Operation("delete", relative_path(path), WritePolicy.MANAGED)


class ProjectYAML:
    """Declare field-level YAML edits; unrelated values survive serialization."""
    def __init__(self, files: Mapping[str, _File]) -> None:
        """Read from the current staged snapshot, never the live filesystem."""
        self._files = files

    def read(self, path: str) -> dict[str, Any]:
        """Return a detached configuration mapping suitable for migration decisions."""
        item = self._files.get(relative_path(path))
        return yaml_mapping(item.content, path) if item else {}

    def set(self, path: str, *, key: tuple[str, ...], value: Any,
            policy: WritePolicy = WritePolicy.IF_MISSING) -> _Operation:
        """Propose a JSON-compatible field value with explicit overwrite policy."""
        copied = json.loads(json.dumps(value, allow_nan=False))
        return _Operation("yaml_set", relative_path(path), policy_value(policy), copied, key_path(key))

    def rename_key(self, path: str, *, source: tuple[str, ...], destination: tuple[str, ...]) -> _Operation:
        """Move a user's existing value without overwriting an occupied destination."""
        return _Operation("yaml_rename", relative_path(path), key=key_path(source), destination=key_path(destination))


@dataclass(frozen=True)
class ProjectContext:
    """Pack hook inputs and read-only staged project views; callbacks are trusted code."""
    name: str
    options: BaseModel | None
    files: ProjectFiles
    yaml: ProjectYAML
