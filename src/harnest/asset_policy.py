"""Declarative Pydantic metadata for durable media storage."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import PurePosixPath
from types import UnionType
from typing import Annotated, Any, Union, get_args, get_origin


_ASSET_STORE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._~-]{0,63}$")


@dataclass(frozen=True, slots=True)
class Stored:
    """Persist a media field using one explicitly named asset storage.

    ``expires_in`` controls the model-facing URL lifetime. ``retention``
    controls stored-byte lifetime; the two durations are deliberately
    independent.
    """

    store: str = "default"
    path: str | None = None
    expires_in: float = 60
    retention: timedelta | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.store, str)
            or not _ASSET_STORE_NAME.fullmatch(self.store)
        ):
            raise ValueError("stored media store must be a valid storage identifier")
        if self.path is not None:
            _validate_path(self.path)
        _positive_duration(self.expires_in, "expires_in")
        if self.retention is not None:
            if not isinstance(self.retention, timedelta):
                raise TypeError("retention must be a datetime.timedelta")
            _positive_duration(self.retention.total_seconds(), "retention")

    def __get_pydantic_json_schema__(self, core_schema: Any, handler: Any) -> Any:
        """Publish non-secret policy so clients can understand the contract."""

        schema = handler(core_schema)
        policy: dict[str, Any] = {
            "store": self.store,
            "expiresIn": self.expires_in,
        }
        if self.path is not None:
            policy["path"] = self.path
        if self.retention is not None:
            policy["retentionSeconds"] = self.retention.total_seconds()
        schema["x-harnest-storage"] = policy
        return schema

    @property
    def retention_seconds(self) -> float | None:
        """Return the store contract's numeric retention representation."""

        if self.retention is None:
            return None
        return self.retention.total_seconds()


def storage_policy(annotation: Any) -> Stored | None:
    """Extract at most one durable storage policy from a nested annotation."""

    found: list[Stored] = []
    _collect(annotation, found)
    unique = tuple(dict.fromkeys(found))
    if len(unique) > 1:
        raise ValueError("an annotation must contain at most one Stored policy")
    return unique[0] if unique else None


def _collect(annotation: Any, found: list[Stored]) -> None:
    if isinstance(annotation, (tuple, list)):
        for item in annotation:
            if isinstance(item, Stored):
                found.append(item)
            else:
                _collect(item, found)
        return
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is Annotated:
        found.extend(item for item in arguments[1:] if isinstance(item, Stored))
        _collect(arguments[0], found)
        return
    if origin in (Union, UnionType, list, tuple, set, frozenset, dict):
        for argument in arguments:
            _collect(argument, found)


def _validate_path(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("stored media path must be a non-empty relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("stored media path must be a safe relative POSIX path")


def _positive_duration(value: object, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be finite and positive")


__all__ = ["Stored", "storage_policy"]
