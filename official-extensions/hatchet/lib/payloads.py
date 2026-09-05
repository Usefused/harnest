"""Bound and snapshot JSON payloads crossing the Hatchet trust boundary."""

from __future__ import annotations

from collections.abc import Mapping
import json
import math
from typing import Any


MAX_HATCHET_PAYLOAD_BYTES = 1_048_576


def normalize_json_mapping(value: Any, label: str) -> dict[str, Any]:
    """Return an independent JSON mapping within the extension's byte budget."""

    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    try:
        # Recursive normalization prevents nested SDK or caller-owned objects
        # from escaping across provider and durable-storage boundaries.
        normalized = _json_value(value, set())
        encoded = json.dumps(
            normalized,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError):
        raise TypeError(f"{label} must contain only JSON values") from None
    if len(encoded) > MAX_HATCHET_PAYLOAD_BYTES:
        raise ValueError(
            f"{label} exceeds the {MAX_HATCHET_PAYLOAD_BYTES}-byte limit"
        )
    return normalized


def _json_value(value: Any, ancestors: set[int]) -> Any:
    """Copy strict JSON values while rejecting cycles and key coercion."""

    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise TypeError("JSON numbers must be finite")
        return value
    if not isinstance(value, (Mapping, list, tuple)):
        raise TypeError("unsupported JSON value")
    return _json_container(value, ancestors)


def _json_container(value: Any, ancestors: set[int]) -> Any:
    """Copy one JSON container while tracking only its active ancestry."""

    identity = id(value)
    if identity in ancestors:
        raise ValueError("cyclic JSON value")
    ancestors.add(identity)
    try:
        if isinstance(value, Mapping):
            if any(type(key) is not str for key in value):
                raise TypeError("JSON object keys must be strings")
            return {
                key: _json_value(item, ancestors)
                for key, item in value.items()
            }
        return [_json_value(item, ancestors) for item in value]
    finally:
        ancestors.remove(identity)


__all__ = ["MAX_HATCHET_PAYLOAD_BYTES", "normalize_json_mapping"]
