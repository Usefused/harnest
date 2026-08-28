"""One JSON normalization boundary for public results and durable state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
import math
from pathlib import Path
from typing import Any, Literal
from uuid import UUID


_NOT_SCALAR = object()


class JSONValueError(TypeError):
    """A value cannot cross a Harnest JSON boundary without losing structure."""


def json_value(
    value: Any, *, unsupported: Literal["error", "string"] = "error"
) -> Any:
    """Return a JSON-safe value or reject unsupported objects explicitly.

    ``unsupported="string"`` is reserved for best-effort diagnostics such as
    the development trace buffer. Public results and durable state use the
    strict default so an authored object cannot silently lose its structure.
    """

    if unsupported not in {"error", "string"}:
        raise ValueError("unsupported JSON policy must be 'error' or 'string'")
    return _normalize(value, unsupported=unsupported, active=set())


def _normalize(value: Any, *, unsupported: str, active: set[int]) -> Any:
    """Normalize one value while detecting recursive containers and models."""

    if isinstance(value, Enum):
        return _normalize(value.value, unsupported=unsupported, active=active)
    scalar = _scalar_value(value)
    if scalar is not _NOT_SCALAR:
        return scalar
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _nested(
            value,
            model_dump(mode="json", by_alias=True),
            unsupported=unsupported,
            active=active,
        )
    if is_dataclass(value) and not isinstance(value, type):
        contents = {field.name: getattr(value, field.name) for field in fields(value)}
        return _nested(value, contents, unsupported=unsupported, active=active)
    if isinstance(value, Mapping):
        return _nested(
            value,
            _mapping_contents(value),
            unsupported=unsupported,
            active=active,
        )
    if isinstance(value, (list, tuple)):
        return _nested(value, list(value), unsupported=unsupported, active=active)
    if unsupported == "string":
        return str(value)
    raise JSONValueError(
        f"unsupported JSON value type {type(value).__name__}; return JSON values, "
        "a dataclass, or a model with model_dump()"
    )


def _scalar_value(value: Any) -> Any:
    """Normalize common scalar types without conflating them with containers."""

    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise JSONValueError("JSON numbers must be finite")
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (UUID, Path)):
        return str(value)
    return _NOT_SCALAR


def _mapping_contents(value: Mapping[Any, Any]) -> dict[str, Any]:
    """Require textual object keys so normalization cannot hide key collisions."""

    invalid = next(
        (key for key in value if not isinstance(key, str)), _NOT_SCALAR
    )
    if invalid is not _NOT_SCALAR:
        raise JSONValueError(
            f"JSON object keys must be strings; got {type(invalid).__name__}"
        )
    return dict(value)


def _nested(
    owner: Any, contents: Any, *, unsupported: str, active: set[int]
) -> Any:
    """Normalize one compound value without allowing reference cycles."""

    identity = id(owner)
    if identity in active:
        raise JSONValueError("recursive values cannot cross a JSON boundary")
    active.add(identity)
    try:
        if isinstance(contents, Mapping):
            return {
                key: _normalize(item, unsupported=unsupported, active=active)
                for key, item in contents.items()
            }
        return [
            _normalize(item, unsupported=unsupported, active=active)
            for item in contents
        ]
    finally:
        active.remove(identity)


__all__ = ["JSONValueError", "json_value"]
