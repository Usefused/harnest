"""Invocation-scoped access to named asset storage capabilities."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
import re
from typing import Any

from .assets import (
    DEFAULT_MAX_ASSET_BYTES,
    AssetNotFoundError,
    AssetRecord,
    AssetScope,
    AssetStorage,
    AssetStoreError,
    AssetURLStorage,
)


_ASSET_STORE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._~-]{0,63}$")


@dataclass(frozen=True, slots=True, repr=False)
class ScopedAssets:
    """Resolve assets only within one authenticated invocation scope."""

    _scope: AssetScope
    _stores: Mapping[str, AssetStorage]

    async def stat(self, reference: Any, *, store: str | None = None) -> AssetRecord:
        """Return safe metadata for an owned asset or fail closed."""

        storage, asset_id = self._resolve(reference, store)
        record = await storage.stat(scope=self._scope, asset_id=asset_id)
        if record is None:
            raise AssetNotFoundError("asset not found")
        return record

    def open(
        self, reference: Any, *, store: str | None = None
    ) -> AsyncIterator[bytes]:
        """Stream owned bytes without exposing the backing storage object."""

        storage, asset_id = self._resolve(reference, store)
        return storage.open(scope=self._scope, asset_id=asset_id)

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

        storage, asset_id = self._resolve(reference, store)
        return await storage.delete(scope=self._scope, asset_id=asset_id)

    async def url(
        self,
        reference: Any,
        *,
        expires_in: float,
        store: str | None = None,
    ) -> str:
        """Ask an explicitly URL-capable storage for a fresh temporary URL."""

        storage, asset_id = self._resolve(reference, store)
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
    ) -> tuple[AssetStorage, str]:
        asset_id = (
            reference
            if isinstance(reference, str)
            else getattr(reference, "asset_id", None)
        )
        if not isinstance(asset_id, str) or not asset_id:
            raise TypeError("asset reference must provide a non-empty asset_id")
        referenced_store = getattr(reference, "store", None)
        name = selected_store or referenced_store or "default"
        if not isinstance(name, str) or not _ASSET_STORE_NAME.fullmatch(name):
            raise ValueError("asset store name must be a valid storage identifier")
        storage = self._stores.get(name)
        if storage is None:
            raise AssetNotFoundError("asset not found")
        return storage, asset_id


__all__ = ["ScopedAssets"]
