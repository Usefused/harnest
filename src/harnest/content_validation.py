"""Resolve authored content references against trusted asset metadata."""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import UnionType
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import BaseModel

from .assets import AssetRecord, AssetScope, AssetStore
from .content import (
    AssetRef,
    Audio,
    AudioConstraints,
    ContentConstraints,
    Data,
    DataConstraints,
    File,
    FileConstraints,
    Image,
    ImageConstraints,
    Video,
    VideoConstraints,
)


class ContentValidationError(ValueError):
    """Reject content without reflecting private references or metadata."""


@dataclass(frozen=True, slots=True)
class ContentCapabilities:
    """Describe content kinds and MIME types accepted by one model boundary."""

    kinds: frozenset[str] = frozenset(
        {"text", "image", "audio", "video", "file", "data", "asset"}
    )
    media_types: frozenset[str] | None = None
    max_asset_bytes: int | None = None

    def __post_init__(self) -> None:
        """Reject unusable capability declarations at adapter construction."""

        known = {"text", "image", "audio", "video", "file", "data", "asset"}
        if not self.kinds or not self.kinds <= known:
            raise ValueError("content capability kinds are invalid")
        if self.max_asset_bytes is not None and self.max_asset_bytes <= 0:
            raise ValueError("content capability max_asset_bytes must be positive")


async def resolve_model_content(
    value: Any,
    *,
    store: AssetStore,
    scope: AssetScope,
    capabilities: ContentCapabilities | None = None,
) -> Any:
    """Return a copy whose content metadata came only from the scoped store."""

    return await _resolve(
        value,
        type(value),
        store=store,
        scope=scope,
        constraints=(),
        capabilities=capabilities or ContentCapabilities(),
    )


async def _resolve(
    value: Any,
    annotation: Any,
    *,
    store: AssetStore,
    scope: AssetScope,
    constraints: tuple[ContentConstraints, ...],
    capabilities: ContentCapabilities,
) -> Any:
    """Walk one validated value while preserving its authored type structure."""

    annotation, local = _annotation_constraints(annotation)
    active = constraints + local
    if isinstance(value, (AssetRef, Image, Audio, Video, File)):
        return await _resolve_asset(value, store, scope, active, capabilities)
    if isinstance(value, Data):
        _validate_data(value, active, capabilities)
        return value
    if isinstance(value, BaseModel):
        updates: dict[str, Any] = {}
        for name, field in type(value).model_fields.items():
            field_annotation = _field_annotation(field.annotation, field.metadata)
            updates[name] = await _resolve(
                getattr(value, name),
                field_annotation,
                store=store,
                scope=scope,
                constraints=(),
                capabilities=capabilities,
            )
        return value.model_copy(update=updates)
    return await _resolve_container(
        value,
        annotation,
        store=store,
        scope=scope,
        constraints=active,
        capabilities=capabilities,
    )


async def _resolve_container(
    value: Any,
    annotation: Any,
    *,
    store: AssetStore,
    scope: AssetScope,
    constraints: tuple[ContentConstraints, ...],
    capabilities: ContentCapabilities,
) -> Any:
    """Rebuild supported containers while resolving their typed children."""

    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if isinstance(value, list):
        item_annotation = arguments[0] if origin is list and arguments else Any
        return await _resolve_sequence(
            value,
            (item_annotation,) * len(value),
            store=store,
            scope=scope,
            constraints=constraints,
            capabilities=capabilities,
        )
    if isinstance(value, tuple):
        item_annotations = _tuple_annotations(arguments, len(value))
        resolved = await _resolve_sequence(
            value,
            item_annotations,
            store=store,
            scope=scope,
            constraints=constraints,
            capabilities=capabilities,
        )
        return tuple(resolved)
    if isinstance(value, dict):
        value_annotation = arguments[1] if origin is dict and len(arguments) == 2 else Any
        return await _resolve_mapping(
            value,
            value_annotation,
            store=store,
            scope=scope,
            constraints=constraints,
            capabilities=capabilities,
        )
    return value


async def _resolve_sequence(
    values: list[Any] | tuple[Any, ...],
    annotations: tuple[Any, ...],
    *,
    store: AssetStore,
    scope: AssetScope,
    constraints: tuple[ContentConstraints, ...],
    capabilities: ContentCapabilities,
) -> list[Any]:
    """Resolve a sequence against one annotation per item."""

    return [
        await _resolve(
            item,
            item_annotation,
            store=store,
            scope=scope,
            constraints=constraints,
            capabilities=capabilities,
        )
        for item, item_annotation in zip(values, annotations)
    ]


async def _resolve_mapping(
    values: dict[Any, Any],
    value_annotation: Any,
    *,
    store: AssetStore,
    scope: AssetScope,
    constraints: tuple[ContentConstraints, ...],
    capabilities: ContentCapabilities,
) -> dict[Any, Any]:
    """Resolve typed mapping values while preserving caller-owned keys."""

    return {
        key: await _resolve(
            item,
            value_annotation,
            store=store,
            scope=scope,
            constraints=constraints,
            capabilities=capabilities,
        )
        for key, item in values.items()
    }


def _annotation_constraints(
    annotation: Any,
) -> tuple[Any, tuple[ContentConstraints, ...]]:
    """Peel one Annotated layer while retaining authored media policy."""

    if get_origin(annotation) is not Annotated:
        return annotation, ()
    arguments = get_args(annotation)
    constraints = tuple(
        item
        for item in arguments[1:]
        if isinstance(
            item,
            (
                ImageConstraints,
                AudioConstraints,
                VideoConstraints,
                FileConstraints,
                DataConstraints,
            ),
        )
    )
    return arguments[0], constraints


def _field_annotation(annotation: Any, metadata: list[Any]) -> Any:
    """Restore field-level Annotated metadata normalized by Pydantic."""

    constraints = tuple(
        item
        for item in metadata
        if isinstance(
            item,
            (
                ImageConstraints,
                AudioConstraints,
                VideoConstraints,
                FileConstraints,
                DataConstraints,
            ),
        )
    )
    return Annotated[annotation, *constraints] if constraints else annotation


def _tuple_annotations(arguments: tuple[Any, ...], size: int) -> tuple[Any, ...]:
    """Expand fixed and variadic tuple annotations for recursive traversal."""

    if len(arguments) == 2 and arguments[1] is Ellipsis:
        return (arguments[0],) * size
    if len(arguments) == size:
        return arguments
    return (Any,) * size


async def _resolve_asset(
    part: AssetRef | Image | Audio | Video | File,
    store: AssetStore,
    scope: AssetScope,
    constraints: tuple[ContentConstraints, ...],
    capabilities: ContentCapabilities,
) -> AssetRef | Image | Audio | Video | File:
    """Authorize one reference, enforce policy, and replace client metadata."""

    record = await store.stat(scope=scope, asset_id=part.asset_id)
    if record is None:
        raise ContentValidationError("content asset is unavailable")
    kind = part.type
    _validate_capabilities(kind, record, capabilities)
    _validate_media_family(kind, record.media_type)
    for constraint in constraints:
        _validate_constraint(kind, record, constraint)
    return _authoritative_part(part, record)


def _validate_capabilities(
    kind: str, record: AssetRecord, capabilities: ContentCapabilities
) -> None:
    """Apply backend limits after server and authored policy have succeeded."""

    if kind not in capabilities.kinds:
        raise ContentValidationError("content kind is not supported by the model")
    if capabilities.media_types is not None and not _mime_allowed(
        record.media_type, capabilities.media_types
    ):
        raise ContentValidationError("content media type is not supported by the model")
    if (
        capabilities.max_asset_bytes is not None
        and record.size_bytes > capabilities.max_asset_bytes
    ):
        raise ContentValidationError("content exceeds the model byte limit")


def _validate_media_family(kind: str, media_type: str) -> None:
    """Prevent a reference discriminator from misrepresenting stored bytes."""

    expected = {"image": "image/", "audio": "audio/", "video": "video/"}
    prefix = expected.get(kind)
    if prefix is not None and not media_type.startswith(prefix):
        raise ContentValidationError("content kind does not match its media type")


def _validate_constraint(
    kind: str, record: AssetRecord, constraint: ContentConstraints
) -> None:
    """Apply one authored policy exclusively to its matching content kind."""

    if constraint._kind != kind:
        raise ContentValidationError("content constraint does not match its field type")
    if constraint.media_types is not None and not _mime_allowed(
        record.media_type, constraint.media_types
    ):
        raise ContentValidationError("content media type is not allowed")
    max_bytes = getattr(constraint, "max_bytes", None)
    if max_bytes is not None and record.size_bytes > max_bytes:
        raise ContentValidationError("content exceeds the authored byte limit")
    metadata = record.metadata
    if isinstance(constraint, ImageConstraints):
        _validate_image(metadata, constraint)
    elif isinstance(constraint, AudioConstraints):
        _validate_audio(metadata, constraint)
    elif isinstance(constraint, VideoConstraints):
        _validate_video(metadata, constraint)
    elif isinstance(constraint, FileConstraints):
        _validate_file(metadata, constraint)


def _mime_allowed(media_type: str, allowed: frozenset[str]) -> bool:
    """Match exact MIME types or a subtype wildcard."""

    return media_type in allowed or f"{media_type.partition('/')[0]}/*" in allowed


def _metadata_value(metadata: Any, name: str, required: bool) -> Any:
    """Fail closed when an authored limit needs unavailable inspection data."""

    value = getattr(metadata, name, None) if metadata is not None else None
    if required and value is None:
        raise ContentValidationError("content metadata required by policy is unavailable")
    return value


def _bounded(value: Any, minimum: Any, maximum: Any) -> bool:
    """Test an optional scalar against its authored inclusive range."""

    return (minimum is None or value >= minimum) and (
        maximum is None or value <= maximum
    )


def _validate_image(metadata: Any, constraint: ImageConstraints) -> None:
    """Enforce trusted dimensions, pixels, ratio, and frame count."""

    width, height = _validated_image_dimensions(metadata, constraint)
    if constraint.max_pixels is not None and width * height > constraint.max_pixels:
        raise ContentValidationError("image pixels exceed authored policy")
    if constraint.min_aspect_ratio is not None or constraint.max_aspect_ratio is not None:
        if not _bounded(
            width / height,
            constraint.min_aspect_ratio,
            constraint.max_aspect_ratio,
        ):
            raise ContentValidationError("image aspect ratio violates authored policy")
    _validate_image_frames(metadata, constraint)


def _validated_image_dimensions(
    metadata: Any, constraint: ImageConstraints
) -> tuple[Any, Any]:
    """Return trusted dimensions after applying authored ranges."""

    required = any(
        value is not None
        for value in (
            constraint.min_width,
            constraint.max_width,
            constraint.min_height,
            constraint.max_height,
            constraint.max_pixels,
            constraint.min_aspect_ratio,
            constraint.max_aspect_ratio,
        )
    )
    width = _metadata_value(metadata, "width", required)
    height = _metadata_value(metadata, "height", required)
    if required and (
        not _bounded(width, constraint.min_width, constraint.max_width)
        or not _bounded(height, constraint.min_height, constraint.max_height)
    ):
        raise ContentValidationError("image dimensions violate authored policy")
    return width, height


def _validate_image_frames(metadata: Any, constraint: ImageConstraints) -> None:
    """Apply frame-count and animation policy to inspected metadata."""

    frames_required = constraint.animated is not None or constraint.max_frames is not None
    frames = _metadata_value(metadata, "frame_count", frames_required)
    if constraint.max_frames is not None and frames > constraint.max_frames:
        raise ContentValidationError("image frame count exceeds authored policy")
    if constraint.animated is not None and (frames > 1) != constraint.animated:
        raise ContentValidationError("image animation violates authored policy")


def _validate_audio(metadata: Any, constraint: AudioConstraints) -> None:
    """Enforce trusted audio duration, sample-rate, and channel limits."""

    _validate_metadata_range(
        metadata,
        "duration_seconds",
        constraint.min_duration_seconds,
        constraint.max_duration_seconds,
        "audio duration violates authored policy",
    )
    _validate_metadata_range(
        metadata,
        "sample_rate_hz",
        constraint.min_sample_rate_hz,
        constraint.max_sample_rate_hz,
        "audio sample rate violates authored policy",
    )
    _validate_metadata_range(
        metadata,
        "channel_count",
        constraint.min_channels,
        constraint.max_channels,
        "audio channel count violates authored policy",
    )


def _validate_video(metadata: Any, constraint: VideoConstraints) -> None:
    """Enforce trusted video dimensions and duration when requested."""

    _validate_metadata_range(
        metadata,
        "duration_seconds",
        constraint.min_duration_seconds,
        constraint.max_duration_seconds,
        "video duration violates authored policy",
    )
    for name, minimum, maximum in (
        ("width", constraint.min_width, constraint.max_width),
        ("height", constraint.min_height, constraint.max_height),
    ):
        _validate_metadata_range(
            metadata, name, minimum, maximum, "video dimensions violate authored policy"
        )
    if constraint.max_pixels is not None:
        width = _metadata_value(metadata, "width", True)
        height = _metadata_value(metadata, "height", True)
        if width * height > constraint.max_pixels:
            raise ContentValidationError("video pixels exceed authored policy")
    # Frame rate and audio presence are deliberately rejected until the trusted
    # inspector records those properties instead of accepting client claims.
    if constraint.min_frame_rate is not None or constraint.max_frame_rate is not None:
        raise ContentValidationError("content metadata required by policy is unavailable")
    if constraint.audio_allowed is not None:
        raise ContentValidationError("content metadata required by policy is unavailable")


def _validate_file(metadata: Any, constraint: FileConstraints) -> None:
    """Enforce inspected page counts and fail closed for archive policy."""

    _validate_metadata_range(
        metadata,
        "page_count",
        None,
        constraint.max_pages,
        "file page count exceeds authored policy",
    )
    if constraint.archives_allowed is not None or constraint.max_expanded_bytes is not None:
        raise ContentValidationError("content metadata required by policy is unavailable")


def _validate_metadata_range(
    metadata: Any,
    name: str,
    minimum: Any,
    maximum: Any,
    message: str,
) -> None:
    """Apply one optional range while requiring authoritative metadata."""

    required = minimum is not None or maximum is not None
    if not required:
        return
    value = _metadata_value(metadata, name, True)
    if not _bounded(value, minimum, maximum):
        raise ContentValidationError(message)


def _validate_data(
    part: Data[Any],
    constraints: tuple[ContentConstraints, ...],
    capabilities: ContentCapabilities,
) -> None:
    """Apply capability and serialized-size limits to custom JSON content."""

    if "data" not in capabilities.kinds:
        raise ContentValidationError("content kind is not supported by the model")
    size = len(
        json.dumps(
            part.model_dump(mode="json", by_alias=True)["value"],
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    for constraint in constraints:
        if not isinstance(constraint, DataConstraints):
            raise ContentValidationError("content constraint does not match its field type")
        if constraint.max_bytes is not None and size > constraint.max_bytes:
            raise ContentValidationError("custom data exceeds the authored byte limit")


def _authoritative_part(
    part: AssetRef | Image | Audio | Video | File, record: AssetRecord
) -> AssetRef | Image | Audio | Video | File:
    """Construct public content from store-owned metadata, discarding claims."""

    common = {
        "assetId": record.asset_id,
        "mediaType": record.media_type,
        "sizeBytes": record.size_bytes,
    }
    metadata = record.metadata
    if isinstance(part, Image):
        return Image.model_validate(
            {
                **common,
                "width": getattr(metadata, "width", None),
                "height": getattr(metadata, "height", None),
                "frameCount": getattr(metadata, "frame_count", None),
            }
        )
    if isinstance(part, Audio):
        return Audio.model_validate(
            {
                **common,
                "durationSeconds": getattr(metadata, "duration_seconds", None),
                "sampleRateHz": getattr(metadata, "sample_rate_hz", None),
                "channels": getattr(metadata, "channel_count", None),
            }
        )
    if isinstance(part, Video):
        return Video.model_validate(
            {
                **common,
                "width": getattr(metadata, "width", None),
                "height": getattr(metadata, "height", None),
                "durationSeconds": getattr(metadata, "duration_seconds", None),
            }
        )
    if isinstance(part, File):
        return File.model_validate(
            {**common, "pageCount": getattr(metadata, "page_count", None)}
        )
    return AssetRef.model_validate(common)


__all__ = [
    "ContentCapabilities",
    "ContentValidationError",
    "resolve_model_content",
]
