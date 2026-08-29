"""Invocation-scoped media leases that keep inline bytes out of history."""

from __future__ import annotations

import base64
import contextvars
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from threading import Lock
from types import UnionType
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import BaseModel

from ._json import json_value
from .asset_policy import Stored
from .assets import AssetMediaMetadata, DEFAULT_MAX_TOTAL_ASSET_BYTES
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
from .stored_media import stored_reference_value


_MARKER_KEY = "harnestTransient"
_LEASE_ID_KEY = "leaseId"
_ATTACHED = "attached"
_BINDINGS: contextvars.ContextVar[
    dict[tuple[int, "TransientMediaScope"], tuple[str, ...]] | None
] = contextvars.ContextVar("harnest_transient_media_bindings", default=None)
_MEDIA_TYPES = (Image, Audio, Video, File)
_CONSTRAINT_TYPES = (
    ImageConstraints,
    AudioConstraints,
    VideoConstraints,
    FileConstraints,
    DataConstraints,
)
_METADATA_TYPES = (*_CONSTRAINT_TYPES, Stored)


class TransientMediaError(ValueError):
    """Reject invalid or unavailable transient media without exposing content."""


@dataclass(frozen=True, slots=True, repr=False)
class TransientMediaScope:
    """Bind private media to one authenticated invocation."""

    user_id: str
    session_id: str
    call_id: str

    def __post_init__(self) -> None:
        """Reject empty scope components before any media is staged."""

        if any(not isinstance(value, str) or not value.strip() for value in (
            self.user_id,
            self.session_id,
            self.call_id,
        )):
            raise ValueError("transient media scope values must be non-empty strings")

    def __repr__(self) -> str:
        """Keep principal and invocation identifiers out of diagnostics."""

        return "TransientMediaScope(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class TransientMediaLease:
    """One inspected byte payload retained only for a pending model call."""

    lease_id: str
    kind: str
    media_type: str
    data: bytes = field(repr=False)
    metadata: AssetMediaMetadata

    def __repr__(self) -> str:
        """Expose only safe format and size aggregates during debugging."""

        return (
            "TransientMediaLease(lease_id=<redacted>, "
            f"kind={self.kind!r}, media_type={self.media_type!r}, "
            f"size_bytes={len(self.data)})"
        )


@dataclass(slots=True)
class _ScopedLease:
    scope: TransientMediaScope
    lease: TransientMediaLease


class TransientMediaLeaseStore:
    """Bound process-local inline media across framework continuation boundaries."""

    def __init__(self, *, max_total_bytes: int = DEFAULT_MAX_TOTAL_ASSET_BYTES) -> None:
        """Configure a hard ceiling shared by all active invocation leases."""

        if not isinstance(max_total_bytes, int) or max_total_bytes < 1:
            raise ValueError("max_total_bytes must be a positive integer")
        self._max_total_bytes = max_total_bytes
        self._total_bytes = 0
        self._leases: dict[str, _ScopedLease] = {}
        self._lock = Lock()

    @property
    def total_bytes(self) -> int:
        """Report active private bytes for bounded runtime diagnostics."""

        with self._lock:
            return self._total_bytes

    def stage(
        self,
        *,
        scope: TransientMediaScope,
        kind: str,
        media_type: str,
        data: bytes,
        metadata: AssetMediaMetadata,
    ) -> TransientMediaLease:
        """Retain inspected immutable bytes and return their private lease."""

        payload = bytes(data)
        with self._lock:
            if self._total_bytes + len(payload) > self._max_total_bytes:
                raise TransientMediaError("transient media capacity exceeded")
            lease_id = self._new_id_locked()
            lease = TransientMediaLease(
                lease_id=lease_id,
                kind=kind,
                media_type=media_type,
                data=payload,
                metadata=metadata,
            )
            self._leases[lease_id] = _ScopedLease(scope=scope, lease=lease)
            self._total_bytes += len(payload)
        return lease

    def peek(
        self, *, scope: TransientMediaScope, lease_id: str
    ) -> TransientMediaLease | None:
        """Read without consuming so provider retries can reuse identical bytes."""

        with self._lock:
            stored = self._leases.get(lease_id)
            if stored is None or stored.scope != scope:
                return None
            return stored.lease

    def pending(self, *, scope: TransientMediaScope) -> tuple[TransientMediaLease, ...]:
        """Return this invocation's leases in stable staging order."""

        with self._lock:
            return tuple(
                stored.lease
                for stored in self._leases.values()
                if stored.scope == scope
            )

    def commit(self, *, scope: TransientMediaScope, lease_ids: Iterable[str]) -> None:
        """Erase acknowledged bytes after a successful terminal model response."""

        with self._lock:
            for lease_id in set(lease_ids):
                stored = self._leases.get(lease_id)
                if stored is not None and stored.scope == scope:
                    self._discard_locked(lease_id, stored)

    def clear(self, *, scope: TransientMediaScope) -> None:
        """Erase every lease when its invocation terminates for any reason."""

        with self._lock:
            owned = [
                (lease_id, stored)
                for lease_id, stored in self._leases.items()
                if stored.scope == scope
            ]
            for lease_id, stored in owned:
                self._discard_locked(lease_id, stored)

    def _new_id_locked(self) -> str:
        """Generate an opaque identifier without allowing collision overwrite."""

        while True:
            lease_id = f"transient_media_{secrets.token_urlsafe(24)}"
            if lease_id not in self._leases:
                return lease_id

    def _discard_locked(self, lease_id: str, stored: _ScopedLease) -> None:
        """Remove one known lease while keeping byte accounting exact."""

        self._leases.pop(lease_id, None)
        self._total_bytes -= len(stored.lease.data)


@dataclass(frozen=True, slots=True)
class TransientMediaAccess:
    """Scoped model-boundary access to the current invocation's private media."""

    store: TransientMediaLeaseStore = field(repr=False)
    scope: TransientMediaScope = field(repr=False)

    def stage(self, value: BaseModel) -> Any:
        """Validate inline fields and return persistence-safe placeholders."""

        staged, lease_ids = stage_transient_media_batch(
            value, store=self.store, scope=self.scope
        )
        self.bind(lease_ids)
        return staged

    def bind(self, lease_ids: Iterable[str]) -> None:
        """Correlate staged bytes with the current execution context only."""

        values = tuple(dict.fromkeys(lease_ids))
        if not values:
            return
        bindings = dict(_BINDINGS.get() or {})
        key = (id(self.store), self.scope)
        bindings[key] = (*bindings.get(key, ()), *values)
        _BINDINGS.set(bindings)

    def peek(self, marker_or_id: Any) -> TransientMediaLease | None:
        """Resolve one private ID without consuming it."""

        lease_id = _coerce_lease_id(marker_or_id)
        if lease_id is None:
            return None
        return self.store.peek(scope=self.scope, lease_id=lease_id)

    def pending(self) -> tuple[TransientMediaLease, ...]:
        """Return bytes pending for this invocation's next matching model call."""

        bindings = _BINDINGS.get() or {}
        lease_ids = bindings.get((id(self.store), self.scope), ())
        return tuple(
            lease
            for lease_id in lease_ids
            if (lease := self.store.peek(scope=self.scope, lease_id=lease_id))
            is not None
        )

    def commit(self, markers_or_ids: Any = None) -> None:
        """Acknowledge selected leases, or every pending lease, after success."""

        if markers_or_ids is None:
            values = tuple(lease.lease_id for lease in self.pending())
        elif isinstance(markers_or_ids, (str, Mapping)):
            values = (markers_or_ids,)
        elif isinstance(markers_or_ids, Iterable):
            values = tuple(markers_or_ids)
        else:
            values = (markers_or_ids,)
        lease_ids = [lease_id for value in values if (lease_id := _coerce_lease_id(value))]
        self.store.commit(scope=self.scope, lease_ids=lease_ids)
        self._forget(lease_ids)

    def clear(self) -> None:
        """Discard all uncommitted bytes for this invocation."""

        self.store.clear(scope=self.scope)
        self._forget(None)

    def _forget(self, lease_ids: Iterable[str] | None) -> None:
        """Remove committed correlation from the current private context."""

        bindings = dict(_BINDINGS.get() or {})
        key = (id(self.store), self.scope)
        if key not in bindings:
            return
        if lease_ids is None:
            bindings.pop(key, None)
        else:
            removed = set(lease_ids)
            remaining = tuple(item for item in bindings[key] if item not in removed)
            if remaining:
                bindings[key] = remaining
            else:
                bindings.pop(key, None)
        _BINDINGS.set(bindings or None)


def stage_transient_media(
    value: BaseModel,
    *,
    store: TransientMediaLeaseStore,
    scope: TransientMediaScope,
) -> Any:
    """Lease inline bytes and return only safe framework-visible placeholders."""

    return stage_transient_media_batch(value, store=store, scope=scope)[0]


def stage_transient_media_batch(
    value: BaseModel,
    *,
    store: TransientMediaLeaseStore,
    scope: TransientMediaScope,
) -> tuple[Any, tuple[str, ...]]:
    """Return safe output plus private IDs for execution-context correlation."""

    staged: list[str] = []
    try:
        result = _stage_value(value, type(value), (), store, scope, staged)
        return result, tuple(staged)
    except Exception:
        # A nested validation failure must roll back siblings already retained.
        store.commit(scope=scope, lease_ids=staged)
        raise


def is_transient_media_marker(value: Any) -> bool:
    """Identify the reserved sanitized marker shape without reading a lease."""

    return transient_media_lease_id(value) is not None


def transient_media_lease_id(value: Any) -> str | None:
    """Return a syntactically valid private lease identifier from one marker."""

    if not isinstance(value, Mapping):
        return None
    marker = value.get(_MARKER_KEY)
    if not isinstance(marker, Mapping) or set(marker) != {_LEASE_ID_KEY}:
        return None
    lease_id = marker.get(_LEASE_ID_KEY)
    if not isinstance(lease_id, str) or not lease_id.startswith("transient_media_"):
        return None
    kind = value.get("type")
    media_type = value.get("mediaType")
    if kind not in {"image", "audio", "video", "file"}:
        return None
    return lease_id if isinstance(media_type, str) else None


def sanitize_transient_media(value: Any) -> tuple[Any, tuple[str, ...]]:
    """Remove legacy private markers while accepting new safe placeholders."""

    lease_ids: list[str] = []

    def walk(item: Any) -> Any:
        lease_id = transient_media_lease_id(item)
        if lease_id is not None:
            lease_ids.append(lease_id)
            return {
                "type": item["type"],
                "mediaType": item["mediaType"],
                "content": "attached",
            }
        if isinstance(item, Mapping):
            return {key: walk(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [walk(child) for child in item]
        return item

    sanitized = walk(value)
    return sanitized, tuple(dict.fromkeys(lease_ids))


def transient_media_placeholder(kind: str, media_type: str) -> dict[str, str]:
    """Build the only transient-media value frameworks may persist."""

    return {"type": kind, "mediaType": media_type, "content": _ATTACHED}


def is_transient_media_placeholder(value: Any) -> bool:
    """Recognize a persistence-safe transient attachment placeholder."""

    return (
        isinstance(value, Mapping)
        and value.get("type") in {"image", "audio", "video", "file"}
        and isinstance(value.get("mediaType"), str)
        and value.get("content") == _ATTACHED
        and _MARKER_KEY not in value
        and _LEASE_ID_KEY not in value
    )


def transient_media_placeholders(value: Any) -> tuple[tuple[str, str], ...]:
    """Collect ordered media signatures without exposing private correlation."""

    found: list[tuple[str, str]] = []

    def walk(item: Any) -> None:
        if is_transient_media_placeholder(item):
            found.append((str(item["type"]), str(item["mediaType"])))
            return
        if isinstance(item, Mapping):
            for child in item.values():
                walk(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child)

    walk(value)
    return tuple(found)


def matching_transient_leases(
    signatures: Iterable[tuple[str, str]],
    leases: Iterable[TransientMediaLease],
) -> tuple[TransientMediaLease, ...]:
    """Match each private pending lease to one visible attachment signature."""

    available = list(signatures)
    matched: list[TransientMediaLease] = []
    for lease in leases:
        signature = (lease.kind, lease.media_type)
        try:
            index = available.index(signature)
        except ValueError:
            continue
        available.pop(index)
        matched.append(lease)
    return tuple(matched)


def _coerce_lease_id(value: Any) -> str | None:
    """Accept either an adapter-discovered marker or an already parsed ID."""

    if isinstance(value, str) and value.startswith("transient_media_"):
        return value
    return transient_media_lease_id(value)


def _stage_value(
    value: Any,
    annotation: Any,
    inherited: tuple[ContentConstraints, ...],
    store: TransientMediaLeaseStore,
    scope: TransientMediaScope,
    staged: list[str],
) -> Any:
    """Walk a validated model while preserving its JSON-facing structure."""

    annotation, local, stored = _annotation_metadata(annotation)
    constraints = inherited + local
    if stored is not None:
        # Durable media is owned by the named AssetStorage pipeline. Leaving
        # it intact here prevents competing transient and durable retention.
        return stored_reference_value(value, stored)
    if isinstance(value, _MEDIA_TYPES):
        return _stage_media(value, constraints, store, scope, staged)
    if isinstance(value, (Data, Text)):
        return json_value(value)
    if isinstance(value, BaseModel):
        return _stage_model(value, store, scope, staged)
    return _stage_container(value, annotation, constraints, store, scope, staged)


def _stage_media(
    value: Image | Audio | Video | File,
    constraints: tuple[ContentConstraints, ...],
    store: TransientMediaLeaseStore,
    scope: TransientMediaScope,
    staged: list[str],
) -> Any:
    """Inspect and lease inline bytes, leaving stored references unchanged."""

    if value.data is None:
        return json_value(value)
    try:
        data = base64.b64decode(value.data, validate=True)
        metadata, media_type = validate_inline_content(value, data, constraints)
        lease = store.stage(
            scope=scope,
            kind=value.type,
            media_type=media_type,
            data=data,
            metadata=metadata,
        )
    except (ValueError, ContentValidationError) as exc:
        raise TransientMediaError("inline media does not satisfy its contract") from exc
    staged.append(lease.lease_id)
    return transient_media_placeholder(lease.kind, lease.media_type)


def _stage_model(
    value: BaseModel,
    store: TransientMediaLeaseStore,
    scope: TransientMediaScope,
    staged: list[str],
) -> dict[str, Any]:
    """Stage authored fields and retain Pydantic's aliases and serializers."""

    result = value.model_dump(mode="json", by_alias=True)
    for name, model_field in type(value).model_fields.items():
        key = model_field.serialization_alias or model_field.alias or name
        annotation = _field_annotation(model_field.annotation, model_field.metadata)
        result[key] = _stage_value(
            getattr(value, name), annotation, (), store, scope, staged
        )
    return result


def _stage_container(
    value: Any,
    annotation: Any,
    constraints: tuple[ContentConstraints, ...],
    store: TransientMediaLeaseStore,
    scope: TransientMediaScope,
    staged: list[str],
) -> Any:
    """Traverse typed containers and normalize unrelated values as JSON."""

    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if isinstance(value, (list, tuple)):
        annotations = _sequence_annotations(origin, arguments, len(value))
        return [
            _stage_value(item, item_annotation, constraints, store, scope, staged)
            for item, item_annotation in zip(value, annotations)
        ]
    if isinstance(value, Mapping):
        child_annotation = arguments[1] if origin is dict and len(arguments) == 2 else Any
        return {
            key: _stage_value(item, child_annotation, constraints, store, scope, staged)
            for key, item in value.items()
        }
    return json_value(value)


def _annotation_metadata(
    annotation: Any,
) -> tuple[Any, tuple[ContentConstraints, ...], Stored | None]:
    """Peel Annotated metadata while retaining validation and storage policy."""

    if get_origin(annotation) is not Annotated:
        return annotation, (), None
    arguments = get_args(annotation)
    metadata = arguments[1:]
    constraints = tuple(
        item for item in metadata if isinstance(item, _CONSTRAINT_TYPES)
    )
    policies = tuple(item for item in metadata if isinstance(item, Stored))
    if len(policies) > 1:
        raise TransientMediaError("media field has multiple storage policies")
    return arguments[0], constraints, policies[0] if policies else None


def _field_annotation(annotation: Any, metadata: list[Any]) -> Any:
    """Restore metadata that Pydantic separates from field annotations."""

    authored = tuple(item for item in metadata if isinstance(item, _METADATA_TYPES))
    return annotation if not authored else Annotated[(annotation, *authored)]


def _sequence_annotations(
    origin: Any, arguments: tuple[Any, ...], size: int
) -> tuple[Any, ...]:
    """Map fixed, variadic, and homogeneous sequence annotations to values."""

    if origin is tuple and len(arguments) == size:
        return arguments
    if origin is tuple and len(arguments) == 2 and arguments[1] is Ellipsis:
        return (arguments[0],) * size
    if origin in (list, tuple) and arguments:
        return (arguments[0],) * size
    return (Any,) * size


__all__ = [
    "TransientMediaAccess",
    "TransientMediaError",
    "TransientMediaLease",
    "TransientMediaLeaseStore",
    "TransientMediaScope",
    "is_transient_media_marker",
    "is_transient_media_placeholder",
    "matching_transient_leases",
    "sanitize_transient_media",
    "stage_transient_media",
    "stage_transient_media_batch",
    "transient_media_lease_id",
    "transient_media_placeholder",
    "transient_media_placeholders",
]
