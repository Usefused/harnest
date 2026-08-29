"""Invocation-scoped access to named asset storage capabilities."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from dataclasses import dataclass, field
import re
from typing import Any

from .assets import (
    DEFAULT_MAX_ASSET_BYTES,
    AssetMediaMetadata,
    AssetNotFoundError,
    AssetRecord,
    AssetScope,
    AssetStorage,
    AssetStoreError,
    AssetURLStorage,
)
from .content import AssetRef
from .logging import get_logger


_ASSET_STORE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._~-]{0,63}$")
_AUDIT = get_logger("asset.audit")


@dataclass(frozen=True, slots=True, repr=False)
class ScopedAssets:
    """Named asset capabilities restricted to one authenticated invocation."""

    _scope: AssetScope
    _stores: Mapping[str, AssetStorage]
    _selected_store: str | None = None
    _invocation_id: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Capture invocation ownership so retained facades fail closed later."""

        active = self._active_context()
        if (
            active.user_id != self._scope.user_id
            or active.session_id != self._scope.session_id
        ):
            raise RuntimeError("asset context does not match the active invocation")
        if self._selected_store is not None:
            _validate_store_name(self._selected_store)
        object.__setattr__(self, "_invocation_id", active.invocation_id)

    def __call__(self, store: str) -> "ScopedAssets":
        """Select one named store without exposing its backing implementation."""

        self._require_active()
        _validate_store_name(store)
        return ScopedAssets(self._scope, self._stores, store)

    async def save(
        self,
        *,
        media_type: str,
        chunks: AsyncIterable[bytes],
        metadata: AssetMediaMetadata | None = None,
        retention_seconds: float | None = None,
        path: str | None = None,
        label: str | None = None,
        store: str | None = None,
    ) -> AssetRef:
        """Persist streamed bytes and return a reference carrying store routing."""

        name, storage = self._storage_for_write(store)
        normalized_label = _normalize_domain_label(label)
        try:
            record = await storage.save(
                scope=self._scope,
                media_type=media_type,
                chunks=self._guarded_chunks(chunks),
                metadata=metadata,
                retention_seconds=retention_seconds,
                path=path,
            )
        except asyncio.CancelledError:
            _audit("save", "failed", name)
            raise
        except BaseException:
            _audit("save", "failed", name)
            raise
        _audit("save", "committed", name)
        return AssetRef(
            assetId=record.asset_id,
            store=name,
            mediaType=record.media_type,
            sizeBytes=record.size_bytes,
            label=normalized_label,
        )

    async def stat(self, reference: Any, *, store: str | None = None) -> AssetRecord:
        """Return safe metadata for an owned asset or fail closed."""

        _, storage, asset_id = self._resolve(reference, store)
        record = await storage.stat(scope=self._scope, asset_id=asset_id)
        if record is None:
            raise AssetNotFoundError("asset not found")
        return record

    def open(
        self, reference: Any, *, store: str | None = None
    ) -> AsyncIterator[bytes]:
        """Stream owned bytes without exposing the backing storage object."""

        _, storage, asset_id = self._resolve(reference, store)

        async def owned_stream() -> AsyncIterator[bytes]:
            # Rechecking each chunk prevents a retained iterator from reading
            # after the invocation which authorized it has completed.
            async for chunk in storage.open(scope=self._scope, asset_id=asset_id):
                self._require_active()
                yield chunk

        return owned_stream()

    async def get(
        self,
        reference: Any,
        *,
        store: str | None = None,
        max_bytes: int = DEFAULT_MAX_ASSET_BYTES,
    ) -> bytes:
        """Read a bounded asset for APIs that require an in-memory payload."""

        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes <= 0
        ):
            raise ValueError("max_bytes must be a positive integer")
        chunks: list[bytes] = []
        size = 0
        async for chunk in self.open(reference, store=store):
            size += len(chunk)
            if size > max_bytes:
                raise AssetStoreError("asset exceeds the requested read limit")
            chunks.append(chunk)
        return b"".join(chunks)

    async def delete(self, reference: Any, *, store: str | None = None) -> bool:
        """Delete an owned asset without revealing another scope's content."""

        name, storage, asset_id = self._resolve(reference, store)
        try:
            deleted = await storage.delete(scope=self._scope, asset_id=asset_id)
        except asyncio.CancelledError:
            _audit("delete", "failed", name)
            raise
        except BaseException:
            _audit("delete", "failed", name)
            raise
        _audit("delete", "committed" if deleted else "not_found", name)
        return deleted

    async def url(
        self,
        reference: Any,
        *,
        expires_in: float,
        store: str | None = None,
    ) -> str:
        """Ask an explicitly URL-capable storage for a fresh temporary URL."""

        _, storage, asset_id = self._resolve(reference, store)
        if not isinstance(storage, AssetURLStorage):
            raise AssetStoreError("asset storage does not support temporary URLs")
        if (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, (int, float))
            or expires_in <= 0
        ):
            raise ValueError("expires_in must be positive")
        value = await storage.signed_url(
            scope=self._scope,
            asset_id=asset_id,
            expires_in=expires_in,
        )
        if not isinstance(value, str) or not value.startswith("https://"):
            raise AssetStoreError("asset storage returned an invalid temporary URL")
        return value

    def _resolve(
        self, reference: Any, selected_store: str | None
    ) -> tuple[str, AssetStorage, str]:
        self._require_active()
        asset_id = (
            reference
            if isinstance(reference, str)
            else getattr(reference, "asset_id", None)
        )
        if not isinstance(asset_id, str) or not asset_id:
            raise TypeError("asset reference must provide a non-empty asset_id")
        name = self._store_name(reference, selected_store)
        storage = self._stores.get(name)
        if storage is None:
            raise AssetNotFoundError("asset not found")
        return name, storage, asset_id

    def _storage_for_write(
        self, selected_store: str | None
    ) -> tuple[str, AssetStorage]:
        """Resolve writes only through an explicit selection or `default`."""

        self._require_active()
        name = _selected_name(selected_store, self._selected_store)
        storage = self._stores.get(name)
        if storage is None:
            raise AssetNotFoundError("asset storage is unavailable")
        return name, storage

    def _store_name(self, reference: Any, selected_store: str | None) -> str:
        """Keep a reference's owning store authoritative during later access."""

        referenced = getattr(reference, "store", None)
        selected = _selected_name(selected_store, self._selected_store, default=None)
        if referenced is not None:
            _validate_store_name(referenced)
        if selected is not None and referenced is not None and selected != referenced:
            # Silent overrides can target an unrelated asset when opaque IDs
            # happen to collide across independent storage backends.
            raise AssetStoreError("asset reference uses the wrong storage")
        return selected or referenced or "default"

    async def _guarded_chunks(
        self, chunks: AsyncIterable[bytes]
    ) -> AsyncIterator[bytes]:
        """Stop an in-flight upload once its invocation authority is revoked."""

        if isinstance(chunks, (bytes, bytearray, memoryview)):
            raise TypeError("asset chunks must be an asynchronous iterable")
        try:
            iterator = chunks.__aiter__()
        except AttributeError:
            raise TypeError("asset chunks must be an asynchronous iterable") from None
        async for chunk in iterator:
            self._require_active()
            yield chunk

    def _active_context(self) -> Any:
        """Resolve the active invocation lazily to avoid a context import cycle."""

        from .context import context

        return context.current()

    def _require_active(self) -> None:
        """Reject retained capability objects outside their owning invocation."""

        active = self._active_context()
        if active.invocation_id != self._invocation_id:
            raise RuntimeError("asset context is no longer active")


def _selected_name(
    explicit: str | None,
    bound: str | None,
    *,
    default: str | None = "default",
) -> str | None:
    """Resolve compatible explicit and bound store selections."""

    if explicit is not None:
        _validate_store_name(explicit)
    if bound is not None:
        _validate_store_name(bound)
    if explicit is not None and bound is not None and explicit != bound:
        raise AssetStoreError("asset context uses a different storage")
    return explicit or bound or default


def _validate_store_name(name: Any) -> None:
    """Validate a public store identifier without reflecting rejected input."""

    if not isinstance(name, str) or not _ASSET_STORE_NAME.fullmatch(name):
        raise ValueError("asset store name must be a valid storage identifier")


def _normalize_domain_label(label: str | None) -> str | None:
    """Keep optional domain meaning separate from the storage routing name."""

    if label is None:
        return None
    if not isinstance(label, str) or not label.strip() or len(label.strip()) > 128:
        raise ValueError("asset label must be 1 to 128 characters")
    return label.strip()


def _audit(operation: str, outcome: str, store: str) -> None:
    """Emit payload-free OTEL-compatible audit attributes after asset writes."""

    # Asset identifiers, labels, content, and ownership remain outside logs.
    _AUDIT.info(
        f"asset.{operation}",
        operation=operation,
        trigger="agent",
        outcome=outcome,
        store=store,
    )


__all__ = ["ScopedAssets"]
