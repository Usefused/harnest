"""Project authored input into LangGraph message content without running a graph."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from ._json import json_value
from .content import AssetRef, Audio, Data, File, Image, Text, Video
from .graph import _model_input_text
from .stored_media import stored_media_reference
from .transient_media import is_transient_media_placeholder, transient_media_lease_id

_CONTENT_PART_TYPES = (Text, Image, Audio, Video, File, Data, AssetRef)
_NO_ORDINARY_CONTENT = object()


def portable_input_content(value: Any) -> str | list[dict[str, Any]]:
    """Preserve authored content order while retaining legacy text inputs."""

    if isinstance(value, _CONTENT_PART_TYPES):
        return [_portable_content_block(value)]
    if _portable_content_sequence(value):
        return [_portable_content_block(item) for item in value]
    if isinstance(value, (BaseModel, Mapping)):
        blocks = _structured_content_blocks(value)
        return blocks or _model_input_text(value)
    return _model_input_text(value)


def _portable_content_sequence(value: Any) -> bool:
    """Recognize a non-empty authored content sequence."""

    return isinstance(value, (list, tuple)) and bool(value) and all(
        isinstance(item, _CONTENT_PART_TYPES) for item in value
    )


def _structured_content_blocks(value: BaseModel | Mapping[str, Any]) -> list[dict[str, Any]]:
    """Project structured input into content blocks with ordinary fields first."""

    ordinary, blocks = _extract_content(value)
    if not blocks:
        return []
    if ordinary is not _NO_ORDINARY_CONTENT:
        blocks.insert(
            0, {"type": "text", "text": _model_input_text(ordinary)}
        )
    return blocks


def _extract_content(value: Any) -> tuple[Any, list[dict[str, Any]]]:
    """Separate nested authored content from its remaining JSON structure."""

    if isinstance(value, _CONTENT_PART_TYPES):
        return _NO_ORDINARY_CONTENT, [_portable_content_block(value)]
    if (
        transient_media_lease_id(value) is not None
        or is_transient_media_placeholder(value)
    ):
        return _NO_ORDINARY_CONTENT, [dict(value)]
    if stored_media_reference(value) is not None:
        return _NO_ORDINARY_CONTENT, [dict(value)]
    if isinstance(value, BaseModel):
        fields = (
            (name, getattr(value, name)) for name in type(value).model_fields
        )
        return _extract_content_mapping(fields)
    if isinstance(value, Mapping):
        return _extract_content_mapping(value.items())
    if isinstance(value, (list, tuple)) and value:
        return _extract_content_sequence(value)
    return json_value(value), []


def _extract_content_mapping(values: Iterable[tuple[str, Any]]) -> tuple[Any, list[dict[str, Any]]]:
    """Walk mapping fields in author order and retain their names in text data."""

    ordinary: dict[str, Any] = {}
    blocks: list[dict[str, Any]] = []
    for name, value in values:
        projected, nested = _extract_content(value)
        if projected is not _NO_ORDINARY_CONTENT:
            ordinary[str(name)] = projected
        blocks.extend(nested)
    return (ordinary if ordinary else _NO_ORDINARY_CONTENT), blocks


def _extract_content_sequence(
    values: Sequence[Any],
) -> tuple[Any, list[dict[str, Any]]]:
    """Walk nested sequences without changing the order of content parts."""

    ordinary: list[Any] = []
    blocks: list[dict[str, Any]] = []
    for value in values:
        projected, nested = _extract_content(value)
        if projected is not _NO_ORDINARY_CONTENT:
            ordinary.append(projected)
        blocks.extend(nested)
    return (ordinary if ordinary else _NO_ORDINARY_CONTENT), blocks


def _portable_content_block(part: Any) -> dict[str, Any]:
    """Serialize one authored part without introducing provider representations."""

    return part.model_dump(mode="json", by_alias=True, exclude_none=True)
