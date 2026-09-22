"""Interpret pack operations and retain ownership of generated files and YAML keys."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import yaml

from .context import LOCK, relative_path, yaml_mapping
from .contracts import ProjectError, WritePolicy, _File, _Operation


MISSING = object()


def digest(value: bytes) -> str:
    """Record generated baselines without copying potentially sensitive values into locks."""
    return hashlib.sha256(value).hexdigest()


def value_digest(value: Any) -> str:
    """Hash a canonical JSON value for field-level drift detection."""
    return digest(json.dumps(value, sort_keys=True, allow_nan=False).encode())


def read_lock(files: dict[str, _File]) -> dict[str, Any]:
    """Validate version and ownership metadata before running any company callback."""
    if LOCK not in files:
        return {"version": 1, "packs": {}, "claims": []}
    try:
        data = json.loads(files[LOCK].content, object_pairs_hook=_unique_pairs)
        _validate_lock(data)
    except (ValueError, TypeError, KeyError) as exc:
        raise ProjectError(f"invalid {LOCK}") from exc
    return data


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Keep duplicate JSON keys from changing lock meaning across consumers."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProjectError("duplicate pack lock key")
        result[key] = value
    return result


def _validate_lock(data: Any) -> None:
    """Reject malformed state rather than silently adopting unknown project versions."""
    if not isinstance(data, dict) or set(data) != {"version", "packs", "claims"} or type(data['version']) is not int or data['version'] != 1:
        raise ProjectError("invalid pack lock shape/version")
    if not isinstance(data['packs'], dict) or not isinstance(data['claims'], list):
        raise ProjectError("invalid pack lock members")
    _validate_versions(data['packs'])
    for claim in data['claims']:
        _validate_claim(claim, data['packs'])


def _validate_versions(packs: dict[str, int]) -> None:
    """Require strictly positive integer versions for every named pack."""
    for name, version in packs.items():
        if not isinstance(name, str) or type(version) is not int or version < 1:
            raise ProjectError("invalid pack schema version")


def _validate_claim(claim: Any, packs: dict[str, int]) -> None:
    """Check every stored ownership path and baseline before it becomes authority."""
    if not isinstance(claim, dict) or set(claim) != {'owner', 'path', 'key', 'digest'}:
        raise ProjectError("invalid ownership record")
    relative_path(claim['path'])
    if claim['owner'] not in packs or not isinstance(claim['key'], list):
        raise ProjectError("unknown pack owner or invalid key")
    if any(not isinstance(k, str) or not k for k in claim['key']):
        raise ProjectError("invalid ownership key")
    value = claim['digest']
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise ProjectError("invalid ownership baseline")


def _overlap(first: list[str], second: list[str]) -> bool:
    """Whole-file and ancestor-key claims overlap all their descendants."""
    length = min(len(first), len(second))
    return first[:length] == second[:length]


class OperationEngine:
    """Apply declarative edits to in-memory files while enforcing pack ownership."""
    def __init__(self, files: dict[str, _File], lock: dict[str, Any]) -> None:
        """Retain the staged content and a separate mutable ownership journal."""
        self.files, self.lock = files, lock
        self.owners: dict[str, set[str]] = {}
        self._touched: list[dict[str, Any]] = []

    def apply(self, owner: str, operation: _Operation) -> None:
        """Check conflicts before any mutation, including no-op proposals."""
        keys = [operation.key]
        if operation.kind == 'yaml_rename':
            keys.append(operation.destination)
        for key in keys:
            self._claim_check(owner, operation.path, list(key))
        handlers = {'write': self._write, 'delete': self._delete,
                    'yaml_set': self._yaml_set, 'yaml_rename': self._yaml_rename}
        handlers[operation.kind](owner, operation)
        self.owners.setdefault(operation.path, set()).add(owner)

    def _claim_check(self, owner: str, path: str, key: list[str]) -> None:
        """Reject cross-pack overlap even when both proposals currently have equal values."""
        for claim in self.lock['claims'] + self._touched:
            if path.startswith(claim['path'] + '/') or claim['path'].startswith(path + '/'):
                raise ProjectError(f"{path}: file/directory conflict with {claim['path']}")
            if claim['path'] == path and claim['owner'] != owner and _overlap(claim['key'], key):
                raise ProjectError(f"{path}: overlapping changes owned by {claim['owner']} and {owner}")
        self._touched.append({'owner': owner, 'path': path, 'key': key})

    def _baseline(self, owner: str, path: str, key: tuple[str, ...]) -> str | None:
        """Find an exact owned baseline; an ancestor claim cannot authorize a field rewrite."""
        for claim in self.lock['claims']:
            if (claim['owner'], claim['path'], claim['key']) == (owner, path, list(key)):
                return claim['digest']
        return None

    def _record(self, owner: str, path: str, key: tuple[str, ...], baseline: str) -> None:
        """Replace ownership evidence only after the corresponding edit succeeds."""
        self._forget(owner, path, key)
        self.lock['claims'].append({'owner': owner, 'path': path, 'key': list(key), 'digest': baseline})

    def _forget(self, owner: str, path: str, key: tuple[str, ...]) -> None:
        """Remove one exact claim while retaining independently owned sibling keys."""
        self.lock['claims'] = [c for c in self.lock['claims']
                               if (c['owner'], c['path'], c['key']) != (owner, path, list(key))]

    def _verify(self, owner: str, path: str, key: tuple[str, ...], current: str) -> None:
        """Require an unchanged owned baseline before replacing user-visible content."""
        if self._baseline(owner, path, key) != current:
            raise ProjectError(f"{path}: local modifications or unowned content; review required")

    def _write(self, owner: str, op: _Operation) -> None:
        """Create files or replace unchanged pack-generated files only."""
        previous = self.files.get(op.path)
        if previous is not None:
            if op.policy == WritePolicy.IF_MISSING:
                return
            self._verify(owner, op.path, (), digest(previous.content))
        self.files[op.path] = _File(op.content, previous.mode if previous else 0o644)
        self._record(owner, op.path, (), digest(op.content))

    def _delete(self, owner: str, op: _Operation) -> None:
        """Delete only a tracked unchanged file; absent files remain idempotent."""
        previous = self.files.get(op.path)
        if previous is not None:
            self._verify(owner, op.path, (), digest(previous.content))
            del self.files[op.path]
        self._forget(owner, op.path, ())

    def _mapping(self, path: str) -> dict[str, Any]:
        """Decode staged YAML without treating an invalid document as empty."""
        previous = self.files.get(path)
        return yaml_mapping(previous.content, path) if previous else {}

    def _save_yaml(self, path: str, data: dict[str, Any]) -> None:
        """Preserve file permissions and unrelated values when serializing YAML."""
        previous = self.files.get(path)
        content = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).encode()
        self.files[path] = _File(content, previous.mode if previous else 0o644)

    def _yaml_set(self, owner: str, op: _Operation) -> None:
        """Update one field without replacing sibling configuration values."""
        data = self._mapping(op.path)
        parent = _parent(data, op.key)
        current = parent.get(op.key[-1], MISSING)
        if current is not MISSING:
            if op.policy == WritePolicy.IF_MISSING:
                return
            self._verify(owner, op.path, op.key, value_digest(current))
        parent[op.key[-1]] = op.content
        self._save_yaml(op.path, data)
        self._record(owner, op.path, op.key, value_digest(op.content))

    def _yaml_rename(self, owner: str, op: _Operation) -> None:
        """Carry user-modified values forward, but never overwrite a destination field."""
        data = self._mapping(op.path)
        source = _parent(data, op.key, create=False)
        if source is None or op.key[-1] not in source:
            return
        if _overlap(list(op.key), list(op.destination)):
            raise ProjectError("rename source and destination must not overlap")
        destination = _parent(data, op.destination)
        if op.destination[-1] in destination:
            raise ProjectError(f"{op.path}: rename destination already exists")
        value = source.pop(op.key[-1])
        destination[op.destination[-1]] = value
        self._save_yaml(op.path, data)
        self._forget(owner, op.path, op.key)
        self._record(owner, op.path, op.destination, value_digest(value))


def _parent(data: dict[str, Any], key: tuple[str, ...], *, create: bool = True) -> dict[str, Any] | None:
    """Traverse object keys without replacing a scalar parent or inventing read paths."""
    cursor = data
    for part in key[:-1]:
        if part not in cursor:
            if not create:
                return None
            cursor[part] = {}
        cursor = cursor[part]
        if not isinstance(cursor, dict):
            raise ProjectError("YAML key parent is not a mapping")
    return cursor
