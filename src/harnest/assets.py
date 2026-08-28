"""Framework-neutral asset storage contracts and a bounded development store."""

from __future__ import annotations

import asyncio
import math
import re
import secrets
from collections.abc import AsyncIterable, AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable


_MEDIA_TYPE_PATTERN = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$"
)
DEFAULT_MAX_ASSET_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_TOTAL_ASSET_BYTES = 100 * 1024 * 1024


class AssetStoreError(RuntimeError):
    """Base error for asset-store operations without sensitive context."""


class AssetNotFoundError(AssetStoreError):
    """Indicate that an asset is absent or inaccessible to the caller."""


class AssetQuotaError(AssetStoreError):
    """Indicate that an asset or store capacity limit was exceeded."""


class AssetValidationError(AssetStoreError, ValueError):
    """Indicate malformed asset input without reflecting the rejected value."""


@dataclass(frozen=True, slots=True, repr=False)
class AssetScope:
    """Bind an asset to one authenticated user and session."""

    user_id: str
    session_id: str

    def __post_init__(self) -> None:
        _require_text(self.user_id, "asset user_id must be a non-empty string")
        _require_text(self.session_id, "asset session_id must be a non-empty string")

    def __repr__(self) -> str:
        """Avoid leaking ownership identifiers through incidental debug output."""

        return "AssetScope(<redacted>)"


@dataclass(frozen=True, slots=True)
class AssetMediaMetadata:
    """Store inspected, non-content media properties safe for policy checks."""

    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    page_count: int | None = None
    frame_count: int | None = None
    channel_count: int | None = None
    sample_rate_hz: int | None = None

    def __post_init__(self) -> None:
        for value in (
            self.width,
            self.height,
            self.page_count,
            self.frame_count,
            self.channel_count,
            self.sample_rate_hz,
        ):
            _require_optional_positive_integer(value)
        _require_optional_nonnegative_float(self.duration_seconds)


@dataclass(frozen=True, slots=True, repr=False)
class AssetRecord:
    """Describe a stored asset without exposing its binary content."""

    asset_id: str
    scope: AssetScope
    media_type: str
    size_bytes: int
    created_at: datetime
    expires_at: datetime | None = None
    metadata: AssetMediaMetadata | None = None

    def __post_init__(self) -> None:
        _require_text(self.asset_id, "asset_id must be a non-empty string")
        _require_media_type(self.media_type)
        _require_scope(self.scope)
        _require_metadata(self.metadata)
        if not isinstance(self.size_bytes, int) or self.size_bytes < 0:
            raise ValueError("asset size_bytes must be a non-negative integer")
        _require_aware_datetime(self.created_at)
        if self.expires_at is not None:
            _require_aware_datetime(self.expires_at)
        if self.expires_at is not None and self.expires_at < self.created_at:
            raise ValueError("asset expires_at must not precede created_at")

    def __repr__(self) -> str:
        """Expose only policy-safe aggregates during interactive inspection."""

        return (
            "AssetRecord(asset_id=<redacted>, scope=<redacted>, "
            f"media_type={self.media_type!r}, size_bytes={self.size_bytes})"
        )


@runtime_checkable
class AssetStore(Protocol):
    """Persist and retrieve binary assets behind scoped opaque references."""

    async def start(self) -> None: ...

    async def save(
        self,
        *,
        scope: AssetScope,
        media_type: str,
        chunks: AsyncIterable[bytes],
        metadata: AssetMediaMetadata | None = None,
        retention_seconds: float | None = None,
    ) -> AssetRecord: ...

    async def stat(
        self, *, scope: AssetScope, asset_id: str
    ) -> AssetRecord | None: ...

    def open(self, *, scope: AssetScope, asset_id: str) -> AsyncIterator[bytes]: ...

    async def delete(self, *, scope: AssetScope, asset_id: str) -> bool: ...

    async def delete_scope(self, *, scope: AssetScope) -> int: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class _StoredAsset:
    record: AssetRecord
    chunks: tuple[bytes, ...]


class MemoryAssetStore:
    """Bounded, process-local asset storage intended for development and tests."""

    def __init__(
        self,
        *,
        max_asset_bytes: int = DEFAULT_MAX_ASSET_BYTES,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_ASSET_BYTES,
        read_chunk_bytes: int = 64 * 1024,
        default_retention_seconds: float | None = None,
        clock: Callable[[], datetime] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        """Configure hard capacities, output chunking, and optional retention."""

        _require_positive_integer(max_asset_bytes, "max_asset_bytes")
        _require_positive_integer(max_total_bytes, "max_total_bytes")
        _require_positive_integer(read_chunk_bytes, "read_chunk_bytes")
        _require_retention(default_retention_seconds)
        self._max_asset_bytes = max_asset_bytes
        self._max_total_bytes = max_total_bytes
        self._read_chunk_bytes = read_chunk_bytes
        self._default_retention_seconds = default_retention_seconds
        self._clock = clock or _utc_now
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(24))
        self._assets: dict[str, _StoredAsset] = {}
        self._total_bytes = 0
        self._reserved_bytes = 0
        self._lock = asyncio.Lock()
        _checked_now(self._clock)

    @property
    def total_bytes(self) -> int:
        """Report committed bytes for development diagnostics."""

        return self._total_bytes

    async def start(self) -> None:
        """Satisfy lifecycle ownership without allocating external resources."""

        return None

    async def save(
        self,
        *,
        scope: AssetScope,
        media_type: str,
        chunks: AsyncIterable[bytes],
        metadata: AssetMediaMetadata | None = None,
        retention_seconds: float | None = None,
    ) -> AssetRecord:
        """Stream an asset into reserved capacity and publish it atomically."""

        _require_scope(scope)
        _require_metadata(metadata)
        normalized_media_type = _require_media_type(media_type)
        retention = self._effective_retention(retention_seconds)
        buffered: list[bytes] = []
        reserved = 0
        try:
            async for chunk in chunks:
                immutable = _require_chunk(chunk)
                reserved = await self._append_reserved(
                    buffered, immutable, reserved
                )
            return await self._commit(
                scope=scope,
                media_type=normalized_media_type,
                chunks=tuple(buffered),
                metadata=metadata,
                retention_seconds=retention,
                reserved_bytes=reserved,
            )
        except asyncio.CancelledError:
            await self._release(reserved)
            raise
        except AssetStoreError:
            await self._release(reserved)
            raise
        except Exception:
            await self._release(reserved)
            raise AssetStoreError("asset upload failed") from None

    async def stat(
        self, *, scope: AssetScope, asset_id: str
    ) -> AssetRecord | None:
        """Return metadata only when the caller owns an unexpired asset."""

        _require_scope(scope)
        async with self._lock:
            self._cleanup_locked(_checked_now(self._clock))
            stored = self._owned_locked(scope, asset_id)
            return stored.record if stored is not None else None

    async def open(
        self, *, scope: AssetScope, asset_id: str
    ) -> AsyncIterator[bytes]:
        """Yield a stable snapshot while later deletion blocks new readers."""

        _require_scope(scope)
        async with self._lock:
            self._cleanup_locked(_checked_now(self._clock))
            stored = self._owned_locked(scope, asset_id)
            if stored is None:
                raise AssetNotFoundError("asset not found")
            chunks = stored.chunks
        for chunk in chunks:
            for offset in range(0, len(chunk), self._read_chunk_bytes):
                yield chunk[offset : offset + self._read_chunk_bytes]

    async def delete(self, *, scope: AssetScope, asset_id: str) -> bool:
        """Delete an owned asset without revealing cross-principal existence."""

        _require_scope(scope)
        async with self._lock:
            self._cleanup_locked(_checked_now(self._clock))
            stored = self._owned_locked(scope, asset_id)
            if stored is None:
                return False
            self._remove_locked(asset_id, stored)
            return True

    async def delete_scope(self, *, scope: AssetScope) -> int:
        """Remove all assets owned by a deleted session without cross-scope reads."""

        async with self._lock:
            self._cleanup_locked(_checked_now(self._clock))
            owned = sorted(
                asset_id
                for asset_id, stored in self._assets.items()
                if stored.record.scope == scope
            )
            for asset_id in owned:
                self._remove_locked(asset_id, self._assets[asset_id])
            return len(owned)

    async def cleanup_expired(self) -> int:
        """Synchronously remove expired assets and return the removal count."""

        async with self._lock:
            return self._cleanup_locked(_checked_now(self._clock))

    async def clear(self) -> None:
        """Deterministically discard all development-store content."""

        async with self._lock:
            self._assets.clear()
            self._total_bytes = 0

    async def close(self) -> None:
        """Discard process-local bytes when the owning application stops."""

        await self.clear()

    def _effective_retention(self, retention_seconds: float | None) -> float | None:
        """Resolve per-write retention without allowing invalid durations."""

        if retention_seconds is None:
            return self._default_retention_seconds
        _require_retention(retention_seconds)
        return retention_seconds

    async def _append_reserved(
        self, buffered: list[bytes], chunk: bytes, reserved: int
    ) -> int:
        """Reserve capacity before retaining the next immutable chunk."""

        next_size = reserved + len(chunk)
        if next_size > self._max_asset_bytes:
            raise AssetQuotaError("asset exceeds per-asset byte limit")
        await self._reserve(len(chunk))
        buffered.append(chunk)
        return next_size

    async def _reserve(self, size_bytes: int) -> None:
        """Account in-flight writes so concurrent streams share one hard ceiling."""

        if size_bytes == 0:
            return
        async with self._lock:
            self._cleanup_locked(_checked_now(self._clock))
            projected = self._total_bytes + self._reserved_bytes + size_bytes
            if projected > self._max_total_bytes:
                raise AssetQuotaError("asset store byte limit exceeded")
            self._reserved_bytes += size_bytes

    async def _release(self, size_bytes: int) -> None:
        """Release capacity reserved by a failed or cancelled upload."""

        if size_bytes == 0:
            return
        async with self._lock:
            self._reserved_bytes -= size_bytes

    async def _commit(
        self,
        *,
        scope: AssetScope,
        media_type: str,
        chunks: tuple[bytes, ...],
        metadata: AssetMediaMetadata | None,
        retention_seconds: float | None,
        reserved_bytes: int,
    ) -> AssetRecord:
        """Publish one fully buffered asset while converting reservation to usage."""

        async with self._lock:
            now = _checked_now(self._clock)
            self._cleanup_locked(now)
            asset_id = self._new_asset_id_locked()
            expires_at = _expiration(now, retention_seconds)
            record = AssetRecord(
                asset_id=asset_id,
                scope=scope,
                media_type=media_type,
                size_bytes=reserved_bytes,
                created_at=now,
                expires_at=expires_at,
                metadata=metadata,
            )
            self._assets[asset_id] = _StoredAsset(record, chunks)
            self._reserved_bytes -= reserved_bytes
            self._total_bytes += reserved_bytes
            return record

    def _owned_locked(
        self, scope: AssetScope, asset_id: str
    ) -> _StoredAsset | None:
        """Collapse absent and unauthorized lookups into one public result."""

        stored = self._assets.get(asset_id)
        if stored is None or stored.record.scope != scope:
            return None
        return stored

    def _new_asset_id_locked(self) -> str:
        """Generate an opaque identifier and tolerate an unlikely collision."""

        for _ in range(32):
            token = self._token_factory()
            if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", token):
                raise AssetStoreError("asset identifier generation failed")
            candidate = f"asset_{token}"
            if candidate not in self._assets:
                return candidate
        raise AssetStoreError("asset identifier generation failed")

    def _cleanup_locked(self, now: datetime) -> int:
        """Remove expired records in deterministic identifier order."""

        expired = sorted(
            asset_id
            for asset_id, stored in self._assets.items()
            if stored.record.expires_at is not None
            and stored.record.expires_at <= now
        )
        for asset_id in expired:
            self._remove_locked(asset_id, self._assets[asset_id])
        return len(expired)

    def _remove_locked(self, asset_id: str, stored: _StoredAsset) -> None:
        """Keep byte accounting coupled to the authoritative removal operation."""

        self._assets.pop(asset_id)
        self._total_bytes -= stored.record.size_bytes


def _require_text(value: object, message: str) -> None:
    """Validate identifiers without including their contents in failures."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(message)


def _require_positive_integer(value: object, name: str) -> None:
    """Validate configurable capacities with a stable redacted error."""

    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _require_optional_positive_integer(value: int | None) -> None:
    """Validate optional inspected media dimensions and counts."""

    if value is not None and (
        not isinstance(value, int) or isinstance(value, bool) or value <= 0
    ):
        raise ValueError("asset media integer metadata must be positive")


def _require_optional_nonnegative_float(value: float | None) -> None:
    """Validate optional duration while excluding booleans and non-finite values."""

    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("asset media duration must be finite and non-negative")


def _require_media_type(value: object) -> str:
    """Normalize a concrete MIME type without reflecting rejected input."""

    if not isinstance(value, str):
        raise ValueError("asset media_type must be a concrete MIME type")
    normalized = value.strip().lower()
    if not _MEDIA_TYPE_PATTERN.fullmatch(normalized):
        raise ValueError("asset media_type must be a concrete MIME type")
    return normalized


def _require_chunk(value: object) -> bytes:
    """Accept immutable or mutable byte chunks without exposing their values."""

    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise AssetValidationError("asset chunks must contain bytes")
    return bytes(value)


def _require_scope(value: object) -> None:
    """Reject unscoped operations before any ownership lookup occurs."""

    if not isinstance(value, AssetScope):
        raise AssetValidationError("asset scope must be an AssetScope")


def _require_metadata(value: object) -> None:
    """Keep arbitrary user content out of the safe metadata boundary."""

    if value is not None and not isinstance(value, AssetMediaMetadata):
        raise AssetValidationError("asset metadata must be inspected media metadata")


def _require_retention(value: float | None) -> None:
    """Validate optional retention as a finite, positive duration."""

    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError("asset retention_seconds must be finite and positive")


def _require_aware_datetime(value: datetime) -> None:
    """Require unambiguous timestamps for retention enforcement."""

    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("asset timestamps must be timezone-aware")


def _checked_now(clock: Callable[[], datetime]) -> datetime:
    """Read and normalize an injected timezone-aware clock."""

    value = clock()
    _require_aware_datetime(value)
    return value.astimezone(timezone.utc)


def _expiration(now: datetime, retention_seconds: float | None) -> datetime | None:
    """Calculate an expiration timestamp after retention has been validated."""

    if retention_seconds is None:
        return None
    return now + timedelta(seconds=retention_seconds)


def _utc_now() -> datetime:
    """Return an aware UTC timestamp for asset lifecycle decisions."""

    return datetime.now(timezone.utc)


__all__ = [
    "AssetMediaMetadata",
    "AssetNotFoundError",
    "AssetQuotaError",
    "AssetRecord",
    "AssetScope",
    "AssetStore",
    "AssetStoreError",
    "DEFAULT_MAX_ASSET_BYTES",
    "DEFAULT_MAX_TOTAL_ASSET_BYTES",
    "AssetValidationError",
    "MemoryAssetStore",
]
