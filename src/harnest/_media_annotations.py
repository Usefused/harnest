"""Shared annotation traversal for transient and stored media staging."""

from __future__ import annotations

from typing import Annotated, Any

from .asset_policy import Stored
from .content import AudioConstraints, DataConstraints, FileConstraints, ImageConstraints, VideoConstraints

CONSTRAINT_TYPES = (ImageConstraints, AudioConstraints, VideoConstraints, FileConstraints, DataConstraints)
_METADATA_TYPES = (*CONSTRAINT_TYPES, Stored)


def field_annotation(annotation: Any, metadata: list[Any]) -> Any:
    """Restore media metadata separated from annotations by Pydantic fields."""
    authored = tuple(item for item in metadata if isinstance(item, _METADATA_TYPES))
    return annotation if not authored else Annotated[(annotation, *authored)]


def sequence_annotations(origin: Any, arguments: tuple[Any, ...], size: int) -> tuple[Any, ...]:
    """Map fixed, variadic, and homogeneous sequence annotations to values."""
    if origin is tuple and len(arguments) == size:
        return arguments
    if origin is tuple and len(arguments) == 2 and arguments[1] is Ellipsis:
        return (arguments[0],) * size
    if origin in (list, tuple) and arguments:
        return (arguments[0],) * size
    return (Any,) * size
