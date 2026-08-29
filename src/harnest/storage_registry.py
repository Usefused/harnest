"""Typed storage capabilities assembled from distributed lifecycle factories."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from types import MappingProxyType
from typing import Any, Mapping, Protocol, runtime_checkable

from .assets import AssetStore
from .checkpoint import ADKStore, CheckpointAuthority
from .session import SessionStore


_STORAGE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._~-]{0,63}$")


@runtime_checkable
class CustomStorage(Protocol):
    """Lifecycle-owned application storage exposed by a stable authored name."""

    async def start(self) -> None:
        """Open the underlying storage resource."""

    async def close(self) -> None:
        """Release the underlying storage resource."""


@dataclass(frozen=True, slots=True)
class StorageRegistry:
    """One normalized storage boundary consumed by compiler and runtime adapters."""

    sessions: SessionStore | ADKStore | None = None
    checkpoints: CheckpointAuthority | None = None
    assets: Mapping[str, AssetStore] = field(default_factory=dict, repr=False)
    custom: Mapping[str, CustomStorage] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Validate contracts and prevent mutation after compilation."""

        _validate_optional(self.sessions, (SessionStore, ADKStore), "sessions")
        _validate_optional(self.checkpoints, CheckpointAuthority, "checkpoints")
        assets = _validated_mapping(self.assets, AssetStore, "assets")
        custom = _validated_mapping(self.custom, CustomStorage, "custom")
        object.__setattr__(self, "assets", MappingProxyType(assets))
        object.__setattr__(self, "custom", MappingProxyType(custom))

    @property
    def default_assets(self) -> AssetStore | None:
        """Return the explicitly named default without choosing by insertion order."""

        return self.assets.get("default")

    def owned_resources(self) -> tuple[Any, ...]:
        """Return each lifecycle-owned object once in deterministic role order."""

        return _unique_by_identity(
            self.sessions,
            self.checkpoints,
            *self.assets.values(),
            *self.custom.values(),
        )


def _validated_mapping(
    values: Mapping[str, Any], expected: type[Any], field_name: str
) -> dict[str, Any]:
    """Validate named storage values without coupling names to one backend type."""

    if not isinstance(values, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    normalized = dict(values)
    for name, value in normalized.items():
        if not isinstance(name, str) or not _STORAGE_NAME.fullmatch(name):
            raise ValueError(f"{field_name} names must be valid storage identifiers")
        if not isinstance(value, expected):
            raise TypeError(f"{field_name}[{name!r}] must implement {expected.__name__}")
    return normalized


def _validate_optional(value: Any, expected: Any, field_name: str) -> None:
    """Validate an optional capability while keeping construction side-effect free."""

    if value is not None and not isinstance(value, expected):
        names = (
            " or ".join(item.__name__ for item in expected)
            if isinstance(expected, tuple)
            else expected.__name__
        )
        raise TypeError(f"{field_name} must implement {names}")


def _unique_by_identity(*values: Any) -> tuple[Any, ...]:
    """Deduplicate aliases because one pool can safely fulfil several roles."""

    result: list[Any] = []
    for value in values:
        if value is not None and all(value is not item for item in result):
            result.append(value)
    return tuple(result)


__all__ = ["CustomStorage", "StorageRegistry"]
