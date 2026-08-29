"""Explicit durable-media staging and model-call-time URL access."""

from __future__ import annotations

import base64
import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, get_args, get_origin

from pydantic import BaseModel

from ._json import json_value
from .asset_policy import Stored
from .assets import AssetScope, AssetStorage
from .content import (
    Audio,
    AudioConstraints,
    ContentConstraints,
    Data,
    DataConstraints,
    File,
    FileConstraints,
    Image,
    ImageConstraints,
    Text,
    Video,
    VideoConstraints,
)
from .content_validation import ContentValidationError, validate_inline_content


_POLICY_KEY = "harnestStored"
_MEDIA_TYPES = (Image, Audio, Video, File)
_CONSTRAINT_TYPES = (
    ImageConstraints,
    AudioConstraints,
    VideoConstraints,
    FileConstraints,
    DataConstraints,
)
_METADATA_TYPES = (*_CONSTRAINT_TYPES, Stored)


class StoredMediaError(ValueError):
    """Reject durable media without reflecting content or storage identifiers."""


@dataclass(frozen=True, slots=True, repr=False)
class StoredMediaReference:
    """One scoped durable reference and its explicit URL delivery policy."""

    kind: str
    store: str
    asset_id: str
    expires_in: float

    def __repr__(self) -> str:
        return (
            "StoredMediaReference(kind="
            f"{self.kind!r}, store=<redacted>, asset_id=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class StoredMediaStage:
    """A staged model plus the exact newly-created assets it owns."""

    value: BaseModel
    _created: tuple[tuple[AssetStorage, str], ...]

    async def rollback(self, *, scope: AssetScope) -> None:
        """Delete only assets created by this staging attempt."""

        await _rollback_created(self._created, scope)


async def stage_stored_media(
    value: BaseModel,
    *,
    stores: Mapping[str, AssetStorage],
    scope: AssetScope,
) -> BaseModel:
    """Persist ``Stored`` fields while preserving their authored Pydantic model."""

    _validate_inline_fields(value, type(value), ())
    return (await stage_stored_media_transaction(
        value, stores=stores, scope=scope
    )).value


async def stage_stored_media_transaction(
    value: BaseModel,
    *,
    stores: Mapping[str, AssetStorage],
    scope: AssetScope,
) -> StoredMediaStage:
    """Stage durable fields and retain precise rollback ownership."""

    saved: list[tuple[AssetStorage, str]] = []
    try:
        staged = await _stage_value(value, type(value), (), stores, scope, saved)
        return StoredMediaStage(staged, tuple(saved))
    except BaseException:
        await _rollback_created(tuple(saved), scope)
        raise


async def _rollback_created(
    created: tuple[tuple[AssetStorage, str], ...], scope: AssetScope
) -> None:
    """Best-effort rollback that remains bounded to this staging transaction."""

    async def discard(storage: AssetStorage, asset_id: str) -> None:
        try:
            await storage.delete(scope=scope, asset_id=asset_id)
        except Exception:
            return

    if created:
        await asyncio.shield(
            asyncio.gather(
                *(discard(storage, asset_id) for storage, asset_id in reversed(created))
            )
        )


def _validate_inline_fields(
    value: Any,
    annotation: Any,
    inherited: tuple[ContentConstraints, ...],
) -> None:
    """Validate every inline sibling before the first durable store write."""

    annotation, local, _ = _annotation_metadata(annotation)
    constraints = inherited + local
    if isinstance(value, _MEDIA_TYPES):
        _validate_inline_media(value, constraints)
        return
    if isinstance(value, BaseModel):
        for name, model_field in type(value).model_fields.items():
            field_annotation = _field_annotation(
                model_field.annotation, model_field.metadata
            )
            _validate_inline_fields(getattr(value, name), field_annotation, ())
        return
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if isinstance(value, (list, tuple)):
        annotations = _sequence_annotations(origin, arguments, len(value))
        for item, item_annotation in zip(value, annotations):
            _validate_inline_fields(item, item_annotation, constraints)
    elif isinstance(value, Mapping):
        child = arguments[1] if origin is dict and len(arguments) == 2 else Any
        for item in value.values():
            _validate_inline_fields(item, child, constraints)


def _validate_inline_media(
    value: Image | Audio | Video | File,
    constraints: tuple[ContentConstraints, ...],
) -> None:
    """Inspect one inline value without retaining bytes or changing storage."""

    if value.data is None:
        return
    try:
        data = base64.b64decode(value.data, validate=True)
        validate_inline_content(value, data, constraints)
    except (ValueError, ContentValidationError) as exc:
        raise StoredMediaError("media does not satisfy its contract") from exc


def stored_media_reference(value: Any) -> StoredMediaReference | None:
    """Parse only the private reference shape emitted by this module."""

    if not isinstance(value, Mapping):
        return None
    policy = _stored_policy(value.get(_POLICY_KEY))
    if policy is None:
        return None
    kind = value.get("type")
    store = value.get("store")
    asset_id = value.get("assetId") or value.get("asset_id")
    if kind not in {"image", "audio", "video", "file"}:
        return None
    if not isinstance(store, str) or not store:
        return None
    if not isinstance(asset_id, str) or not asset_id:
        return None
    return StoredMediaReference(kind, store, asset_id, policy)


def _stored_policy(value: Any) -> float | None:
    """Parse one strict non-secret delivery policy marker."""

    if not isinstance(value, Mapping) or set(value) != {"expiresIn"}:
        return None
    expires_in = value.get("expiresIn")
    if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
        return None
    return float(expires_in) if expires_in > 0 else None


def stored_media_references(value: Any) -> tuple[StoredMediaReference, ...]:
    """Find durable references nested in framework tool-result JSON."""

    found: list[StoredMediaReference] = []

    def walk(item: Any) -> None:
        reference = stored_media_reference(item)
        if reference is not None:
            found.append(reference)
            return
        if isinstance(item, Mapping):
            for child in item.values():
                walk(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child)

    walk(value)
    return tuple(found)


def stored_reference_value(value: Any, policy: Stored) -> dict[str, Any]:
    """Serialize one already-stored media model with its private delivery policy."""

    reference = json_value(value)
    if not isinstance(reference, dict) or not isinstance(reference.get("assetId"), str):
        raise StoredMediaError("stored media did not produce an asset reference")
    reference.pop("data", None)
    reference[_POLICY_KEY] = {"expiresIn": policy.expires_in}
    return reference


def sanitize_stored_media(value: Any) -> Any:
    """Remove private delivery metadata while retaining the scoped asset reference."""

    if isinstance(value, Mapping):
        return {
            key: sanitize_stored_media(child)
            for key, child in value.items()
            if key != _POLICY_KEY
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_stored_media(child) for child in value]
    return value


def model_uses_stored(model: type[BaseModel]) -> bool:
    """Return whether a Pydantic contract contains any ``Stored`` field."""

    seen: set[type[BaseModel]] = set()

    def inspect_annotation(annotation: Any, metadata: tuple[Any, ...] = ()) -> bool:
        if any(isinstance(item, Stored) for item in metadata):
            return True
        if get_origin(annotation) is Annotated:
            arguments = get_args(annotation)
            return inspect_annotation(arguments[0], tuple(arguments[1:]))
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            if annotation in seen:
                return False
            seen.add(annotation)
            return any(
                inspect_annotation(field.annotation, tuple(field.metadata))
                for field in annotation.model_fields.values()
            )
        return any(inspect_annotation(item) for item in get_args(annotation))

    return inspect_annotation(model)


async def _stage_value(
    value: Any,
    annotation: Any,
    inherited: tuple[ContentConstraints, ...],
    stores: Mapping[str, AssetStorage],
    scope: AssetScope,
    saved: list[tuple[AssetStorage, str]],
) -> Any:
    annotation, local, policy = _annotation_metadata(annotation)
    constraints = inherited + local
    if policy is not None:
        if not isinstance(value, _MEDIA_TYPES):
            raise StoredMediaError("Stored may annotate only media content")
        return await _stage_media(value, constraints, policy, stores, scope, saved)
    if isinstance(value, BaseModel):
        return await _stage_model(value, stores, scope, saved)
    if isinstance(value, (Data, Text, *_MEDIA_TYPES)):
        return value
    return await _stage_container(
        value, annotation, constraints, stores, scope, saved
    )


async def _stage_media(
    value: Image | Audio | Video | File,
    constraints: tuple[ContentConstraints, ...],
    policy: Stored,
    stores: Mapping[str, AssetStorage],
    scope: AssetScope,
    saved: list[tuple[AssetStorage, str]],
) -> Image | Audio | Video | File:
    storage = stores.get(policy.store)
    if storage is None:
        raise StoredMediaError("declared asset storage is unavailable")
    record = await _stored_record(value, constraints, policy, storage, scope)
    if value.data is not None:
        saved.append((storage, record.asset_id))
    return _stored_model(value, policy, record)


async def _stored_record(
    value: Image | Audio | Video | File,
    constraints: tuple[ContentConstraints, ...],
    policy: Stored,
    storage: AssetStorage,
    scope: AssetScope,
) -> Any:
    """Authorize an existing reference or save one inspected inline payload."""

    if value.data is None:
        if value.asset_id is None or value.store != policy.store:
            raise StoredMediaError("stored media reference uses the wrong storage")
        record = await storage.stat(scope=scope, asset_id=value.asset_id)
        if record is None:
            raise StoredMediaError("stored media reference is unavailable")
        return record
    try:
        data = base64.b64decode(value.data, validate=True)
        metadata, media_type = validate_inline_content(value, data, constraints)
        return await storage.save(
            scope=scope,
            media_type=media_type,
            chunks=_one_chunk(data),
            metadata=metadata,
            retention_seconds=policy.retention_seconds,
            path=policy.path,
        )
    except (ValueError, ContentValidationError) as exc:
        raise StoredMediaError("stored media does not satisfy its contract") from exc


def _stored_model(value: Any, policy: Stored, record: Any) -> Any:
    """Rebuild one authored media type from authoritative stored metadata."""

    payload: dict[str, Any] = {
        "type": value.type,
        "store": policy.store,
        "assetId": record.asset_id,
        "mediaType": record.media_type,
        "sizeBytes": record.size_bytes,
    }
    metadata = record.metadata
    if metadata is not None:
        for source, target in (
            ("width", "width"),
            ("height", "height"),
            ("duration_seconds", "durationSeconds"),
            ("page_count", "pageCount"),
            ("frame_count", "frameCount"),
            ("channel_count", "channels"),
            ("sample_rate_hz", "sampleRateHz"),
        ):
            item = getattr(metadata, source, None)
            if item is not None and target in type(value).model_fields:
                payload[target] = item
    return type(value).model_validate(payload)


async def _one_chunk(data: bytes):
    yield data


async def _stage_model(
    value: BaseModel,
    stores: Mapping[str, AssetStorage],
    scope: AssetScope,
    saved: list[tuple[AssetStorage, str]],
) -> BaseModel:
    updates: dict[str, Any] = {}
    for name, model_field in type(value).model_fields.items():
        annotation = _field_annotation(model_field.annotation, model_field.metadata)
        updates[name] = await _stage_value(
            getattr(value, name), annotation, (), stores, scope, saved
        )
    return value.model_copy(update=updates)


async def _stage_container(
    value: Any,
    annotation: Any,
    constraints: tuple[ContentConstraints, ...],
    stores: Mapping[str, AssetStorage],
    scope: AssetScope,
    saved: list[tuple[AssetStorage, str]],
) -> Any:
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if isinstance(value, (list, tuple)):
        annotations = _sequence_annotations(origin, arguments, len(value))
        projected = [
            await _stage_value(item, item_annotation, constraints, stores, scope, saved)
            for item, item_annotation in zip(value, annotations)
        ]
        return tuple(projected) if isinstance(value, tuple) else projected
    if isinstance(value, Mapping):
        child = arguments[1] if origin is dict and len(arguments) == 2 else Any
        return {
            key: await _stage_value(item, child, constraints, stores, scope, saved)
            for key, item in value.items()
        }
    return value


def _annotation_metadata(
    annotation: Any,
) -> tuple[Any, tuple[ContentConstraints, ...], Stored | None]:
    if get_origin(annotation) is not Annotated:
        return annotation, (), None
    arguments = get_args(annotation)
    constraints = tuple(
        item for item in arguments[1:] if isinstance(item, _CONSTRAINT_TYPES)
    )
    policies = tuple(item for item in arguments[1:] if isinstance(item, Stored))
    if len(policies) > 1:
        raise StoredMediaError("media field has multiple storage policies")
    return arguments[0], constraints, policies[0] if policies else None


def _field_annotation(annotation: Any, metadata: list[Any]) -> Any:
    authored = tuple(item for item in metadata if isinstance(item, _METADATA_TYPES))
    return annotation if not authored else Annotated[(annotation, *authored)]


def _sequence_annotations(
    origin: Any, arguments: tuple[Any, ...], size: int
) -> tuple[Any, ...]:
    if origin is tuple and len(arguments) == size:
        return arguments
    if origin is tuple and len(arguments) == 2 and arguments[1] is Ellipsis:
        return (arguments[0],) * size
    if origin in (list, tuple) and arguments:
        return (arguments[0],) * size
    return (Any,) * size


__all__ = [
    "StoredMediaError",
    "StoredMediaReference",
    "StoredMediaStage",
    "sanitize_stored_media",
    "model_uses_stored",
    "stage_stored_media",
    "stage_stored_media_transaction",
    "stored_reference_value",
    "stored_media_reference",
    "stored_media_references",
]
