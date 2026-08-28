"""Portable, reference-only multimodal content contracts."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from types import UnionType
from typing import (
    Annotated,
    Any,
    ClassVar,
    Generic,
    Literal,
    TypeAlias,
    TypeVar,
    Union,
    get_args,
    get_origin,
)

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ``*`` is a valid HTTP token character, but reserved here for whole-component
# wildcards so patterns cannot express ambiguous values such as ``*/png``.
_MIME_TOKEN = r"[!#$%&'+.^_`|~A-Za-z0-9-]+"
_MIME_PATTERN = re.compile(rf"^(?:\*/\*|{_MIME_TOKEN}/(?:\*|{_MIME_TOKEN}))$")
_ASSET_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,254}$"
_CAMEL_BOUNDARY = re.compile(r"_([a-z])")


class _PortableModel(BaseModel):
    """Reject undeclared payload fields and keep message values immutable."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )


class Text(_PortableModel):
    """An ordered UTF-8 text part in a portable message."""

    type: Literal["text"] = "text"
    text: str = Field(min_length=1)


class _AssetContent(_PortableModel):
    """Shared authoritative metadata for a reference-only stored asset."""

    asset_id: str = Field(alias="assetId", pattern=_ASSET_ID_PATTERN)
    media_type: str | None = Field(default=None, alias="mediaType")
    size_bytes: int | None = Field(default=None, alias="sizeBytes", ge=0)

    @field_validator("media_type")
    @classmethod
    def _validate_media_type(cls, value: str | None) -> str | None:
        """Require a concrete MIME type on resolved asset metadata."""

        if value is not None and (not _valid_mime_pattern(value) or "*" in value):
            raise ValueError("media_type must be a concrete MIME type")
        return value.lower() if value is not None else None


class AssetRef(_AssetContent):
    """A backend-neutral reference to bytes held by an asset store."""

    type: Literal["asset"] = "asset"


class Image(_AssetContent):
    """A stored image reference with optional authoritative dimensions."""

    type: Literal["image"] = "image"
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    animated: bool | None = None
    frame_count: int | None = Field(default=None, alias="frameCount", gt=0)


class Audio(_AssetContent):
    """A stored audio reference with optional authoritative media metadata."""

    type: Literal["audio"] = "audio"
    duration_seconds: float | None = Field(
        default=None, alias="durationSeconds", ge=0, allow_inf_nan=False
    )
    sample_rate_hz: int | None = Field(default=None, alias="sampleRateHz", gt=0)
    channels: int | None = Field(default=None, gt=0)


class Video(_AssetContent):
    """A stored video reference with optional authoritative media metadata."""

    type: Literal["video"] = "video"
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    duration_seconds: float | None = Field(
        default=None, alias="durationSeconds", ge=0, allow_inf_nan=False
    )
    frame_rate: float | None = Field(
        default=None, alias="frameRate", gt=0, allow_inf_nan=False
    )
    has_audio: bool | None = Field(default=None, alias="hasAudio")


class File(_AssetContent):
    """A stored file reference with safe display and document metadata."""

    type: Literal["file"] = "file"
    name: str | None = Field(default=None, min_length=1, max_length=255)
    page_count: int | None = Field(default=None, alias="pageCount", gt=0)


DataValue = TypeVar("DataValue")


class Data(_PortableModel, Generic[DataValue]):
    """Typed JSON application data carried as an ordered content part."""

    type: Literal["data"] = "data"
    value: DataValue

    @model_validator(mode="after")
    def _require_json_data(self) -> "Data[DataValue]":
        """Fail at authoring boundaries when custom data cannot travel as JSON."""

        try:
            json.dumps(_json_value(self.value), allow_nan=False)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("data value must be JSON-serializable") from exc
        return self


def _json_value(value: Any) -> Any:
    """Project Pydantic values to JSON-compatible Python before validation."""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    return value


def _valid_mime_pattern(value: Any) -> bool:
    """Accept exact MIME types and a wildcard only for the subtype."""

    return isinstance(value, str) and bool(_MIME_PATTERN.fullmatch(value))


def _positive(name: str, value: int | float | None) -> None:
    """Validate optional limits whose zero value would be meaningless."""

    if value is not None and (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be positive")


def _positive_integer(name: str, value: int | None) -> None:
    """Require integer-only count, byte, and dimension limits."""

    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
    ):
        raise ValueError(f"{name} must be a positive integer")


def _nonnegative(name: str, value: int | float | None) -> None:
    """Validate optional lower bounds that legitimately permit zero."""

    if value is not None and (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{name} must be nonnegative")


def _optional_bool(name: str, value: bool | None) -> None:
    """Prevent truthy authored values from silently changing media policy."""

    if value is not None and not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")


def _ordered(
    minimum_name: str,
    minimum: int | float | None,
    maximum_name: str,
    maximum: int | float | None,
) -> None:
    """Reject an authored range whose maximum is below its minimum."""

    if minimum is not None and maximum is not None and maximum < minimum:
        raise ValueError(f"{maximum_name} must be at least {minimum_name}")


@dataclass(frozen=True, slots=True)
class _ContentConstraints:
    """Base immutable metadata shared by authored content constraints."""

    media_types: frozenset[str] | None = None
    _kind: ClassVar[str]

    def __post_init__(self) -> None:
        """Normalize and validate authored MIME allowlists."""

        if self.media_types is None:
            return
        normalized = frozenset(
            value.lower() if isinstance(value, str) else value
            for value in self.media_types
        )
        if not normalized or any(not _valid_mime_pattern(value) for value in normalized):
            raise ValueError(
                "media_types must contain valid MIME types or subtype wildcards"
            )
        object.__setattr__(self, "media_types", normalized)

    def __get_pydantic_json_schema__(self, core_schema: Any, handler: Any) -> Any:
        """Publish authored limits in schemas without changing value validation."""

        schema = handler(core_schema)
        schema.update(constraint_schema_extension(self))
        return schema


@dataclass(frozen=True, slots=True)
class ImageConstraints(_ContentConstraints):
    """Reusable limits for an image field declared with ``Annotated``."""

    max_bytes: int | None = None
    min_width: int | None = None
    max_width: int | None = None
    min_height: int | None = None
    max_height: int | None = None
    max_pixels: int | None = None
    min_aspect_ratio: float | None = None
    max_aspect_ratio: float | None = None
    animated: bool | None = None
    max_frames: int | None = None
    _kind: ClassVar[str] = "image"

    def __post_init__(self) -> None:
        """Validate image bounds before the alias is used by an agent model."""

        super(ImageConstraints, self).__post_init__()
        for name in (
            "max_bytes",
            "min_width",
            "max_width",
            "min_height",
            "max_height",
            "max_pixels",
            "max_frames",
        ):
            _positive_integer(name, getattr(self, name))
        _positive("min_aspect_ratio", self.min_aspect_ratio)
        _positive("max_aspect_ratio", self.max_aspect_ratio)
        _optional_bool("animated", self.animated)
        _ordered("min_width", self.min_width, "max_width", self.max_width)
        _ordered("min_height", self.min_height, "max_height", self.max_height)
        _ordered(
            "min_aspect_ratio",
            self.min_aspect_ratio,
            "max_aspect_ratio",
            self.max_aspect_ratio,
        )
        if self.animated is False and self.max_frames not in (None, 1):
            raise ValueError("max_frames must be 1 when animated is false")


@dataclass(frozen=True, slots=True)
class AudioConstraints(_ContentConstraints):
    """Reusable limits for an audio field declared with ``Annotated``."""

    max_bytes: int | None = None
    min_duration_seconds: float | None = None
    max_duration_seconds: float | None = None
    min_sample_rate_hz: int | None = None
    max_sample_rate_hz: int | None = None
    min_channels: int | None = None
    max_channels: int | None = None
    _kind: ClassVar[str] = "audio"

    def __post_init__(self) -> None:
        """Validate audio bounds before the alias is used by an agent model."""

        super(AudioConstraints, self).__post_init__()
        _positive_integer("max_bytes", self.max_bytes)
        _nonnegative("min_duration_seconds", self.min_duration_seconds)
        _positive("max_duration_seconds", self.max_duration_seconds)
        for name in (
            "min_sample_rate_hz",
            "max_sample_rate_hz",
            "min_channels",
            "max_channels",
        ):
            _positive_integer(name, getattr(self, name))
        _ordered(
            "min_duration_seconds",
            self.min_duration_seconds,
            "max_duration_seconds",
            self.max_duration_seconds,
        )
        _ordered(
            "min_sample_rate_hz",
            self.min_sample_rate_hz,
            "max_sample_rate_hz",
            self.max_sample_rate_hz,
        )
        _ordered("min_channels", self.min_channels, "max_channels", self.max_channels)


@dataclass(frozen=True, slots=True)
class VideoConstraints(_ContentConstraints):
    """Reusable limits for a video field declared with ``Annotated``."""

    max_bytes: int | None = None
    min_duration_seconds: float | None = None
    max_duration_seconds: float | None = None
    min_width: int | None = None
    max_width: int | None = None
    min_height: int | None = None
    max_height: int | None = None
    max_pixels: int | None = None
    min_frame_rate: float | None = None
    max_frame_rate: float | None = None
    audio_allowed: bool | None = None
    _kind: ClassVar[str] = "video"

    def __post_init__(self) -> None:
        """Validate video bounds before the alias is used by an agent model."""

        super(VideoConstraints, self).__post_init__()
        for name in (
            "max_bytes",
            "min_width",
            "max_width",
            "min_height",
            "max_height",
            "max_pixels",
        ):
            _positive_integer(name, getattr(self, name))
        _positive("max_duration_seconds", self.max_duration_seconds)
        _positive("min_frame_rate", self.min_frame_rate)
        _positive("max_frame_rate", self.max_frame_rate)
        _nonnegative("min_duration_seconds", self.min_duration_seconds)
        _optional_bool("audio_allowed", self.audio_allowed)
        _ordered(
            "min_duration_seconds",
            self.min_duration_seconds,
            "max_duration_seconds",
            self.max_duration_seconds,
        )
        _ordered("min_width", self.min_width, "max_width", self.max_width)
        _ordered("min_height", self.min_height, "max_height", self.max_height)
        _ordered(
            "min_frame_rate",
            self.min_frame_rate,
            "max_frame_rate",
            self.max_frame_rate,
        )


@dataclass(frozen=True, slots=True)
class FileConstraints(_ContentConstraints):
    """Reusable limits for a file field declared with ``Annotated``."""

    max_bytes: int | None = None
    max_pages: int | None = None
    archives_allowed: bool | None = None
    max_expanded_bytes: int | None = None
    _kind: ClassVar[str] = "file"

    def __post_init__(self) -> None:
        """Validate file bounds before the alias is used by an agent model."""

        super(FileConstraints, self).__post_init__()
        _positive_integer("max_bytes", self.max_bytes)
        _positive_integer("max_pages", self.max_pages)
        _positive_integer("max_expanded_bytes", self.max_expanded_bytes)
        _optional_bool("archives_allowed", self.archives_allowed)
        if self.archives_allowed is False and self.max_expanded_bytes is not None:
            raise ValueError(
                "max_expanded_bytes requires archives_allowed to be true or unset"
            )


@dataclass(frozen=True, slots=True)
class DataConstraints(_ContentConstraints):
    """Reusable serialized-size limits for typed JSON application data."""

    max_bytes: int | None = None
    _kind: ClassVar[str] = "data"

    def __post_init__(self) -> None:
        """Validate custom-data bounds before use in an agent model."""

        super(DataConstraints, self).__post_init__()
        _positive_integer("max_bytes", self.max_bytes)


ContentConstraints: TypeAlias = (
    ImageConstraints
    | AudioConstraints
    | VideoConstraints
    | FileConstraints
    | DataConstraints
)


def content_constraints(annotation: Any) -> tuple[ContentConstraints, ...]:
    """Extract constraint metadata recursively from an authored annotation."""

    found: list[ContentConstraints] = []
    _collect_constraints(annotation, found)
    # Preserve authored order while avoiding duplicate traversal through aliases.
    return tuple(dict.fromkeys(found))


def _collect_constraints(annotation: Any, found: list[ContentConstraints]) -> None:
    """Walk ``Annotated`` and container arguments without interpreting values."""

    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is Annotated:
        found.extend(
            item for item in arguments[1:] if isinstance(item, _ContentConstraints)
        )
        _collect_constraints(arguments[0], found)
        return
    if origin in (Union, UnionType, list, tuple, set, frozenset, dict):
        for argument in arguments:
            _collect_constraints(argument, found)


def constraint_schema_extension(constraint: ContentConstraints) -> dict[str, Any]:
    """Render one immutable constraint as the public JSON Schema extension."""

    values = asdict(constraint)
    payload = {
        _camel_case(name): _schema_value(value)
        for name, value in values.items()
        if value is not None
    }
    payload["kind"] = constraint._kind
    return {"x-harnest-media": payload}


def content_schema_extension(annotation: Any) -> dict[str, Any]:
    """Render an annotation's single constraint as ``x-harnest-media``."""

    constraints = content_constraints(annotation)
    if not constraints:
        return {}
    if len(constraints) != 1:
        raise ValueError("an annotation must contain at most one content constraint")
    return constraint_schema_extension(constraints[0])


def _camel_case(value: str) -> str:
    """Use the same lower-camel spelling as Harnest wire contracts."""

    return _CAMEL_BOUNDARY.sub(lambda match: match.group(1).upper(), value)


def _schema_value(value: Any) -> Any:
    """Stabilize unordered metadata so generated schemas are deterministic."""

    if isinstance(value, frozenset):
        return sorted(value)
    return value


ContentPart: TypeAlias = Annotated[
    Text | Image | Audio | Video | File | Data[Any] | AssetRef,
    Field(discriminator="type"),
]


__all__ = [
    "AssetRef",
    "Audio",
    "AudioConstraints",
    "ContentConstraints",
    "ContentPart",
    "Data",
    "DataConstraints",
    "File",
    "FileConstraints",
    "Image",
    "ImageConstraints",
    "Text",
    "Video",
    "VideoConstraints",
    "constraint_schema_extension",
    "content_constraints",
    "content_schema_extension",
]
