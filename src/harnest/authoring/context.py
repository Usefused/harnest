"""Read-only views and operation builders passed to trusted project pack hooks."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
from string import Template
from typing import Any, Mapping

import yaml
from pydantic import BaseModel

from .contracts import ProjectError, WritePolicy, _File, _Operation


IGNORED = frozenset({".git", ".harnest", ".venv", "__pycache__", ".pytest_cache", ".cache"})
LOCK = "harnest-packs.lock"


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
        if self._templates is None:
            raise ProjectError("pack has no template directory")
        source = self._templates / relative_path(template)
        if not source.resolve().is_relative_to(self._templates.resolve()):
            raise ProjectError("template escapes its package directory")
        content = source.read_text(encoding="utf-8")
        if values is not None:
            content = Template(content).substitute(values)
        return self.write_text(path, content, policy=policy)

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
