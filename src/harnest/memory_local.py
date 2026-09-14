"""Reference explicit-memory storage for tests, not durable deployments."""

import asyncio
from dataclasses import replace
import time

from .memory import MemoryCleanupPage, MemoryPage, MemoryRecord, MemoryScope
from .memory_provider import MemoryProvider, decode_record, encode_record, revision_check


class InMemoryStore(MemoryProvider):
    """Implement memory in process-local storage that is lost on process exit."""

    def __init__(self) -> None:
        """Create an isolated reference store with atomic mutation ownership."""
        self._records: dict[tuple[MemoryScope, str], str] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """The process-local reference provider needs no external resources."""

    async def close(self) -> None:
        """Release no external resources; retained test records stay inspectable."""

    def _live(self, scope: MemoryScope, key: str) -> MemoryRecord | None:
        """Hide expired records while removing their in-process index entry."""
        raw = self._records.get((scope, key))
        if raw is None:
            return None
        record = decode_record(raw)
        if record.expires_at is not None and record.expires_at <= time.time():
            self._records.pop((scope, key), None)
            return None
        return record

    async def _put(self, record: MemoryRecord, expected: str | None) -> MemoryRecord:
        """Update content and revision in the same critical section."""
        async with self._lock:
            current = self._live(record.scope, record.key)
            revision_check(expected, current)
            if current is not None:
                record = replace(record, created_at=current.created_at)
            self._records[(record.scope, record.key)] = encode_record(record)
            return decode_record(encode_record(record))

    async def _read(self, scope: MemoryScope, key: str | None, query: str | None, limit: int, after: str | None) -> MemoryPage:
        """Mirror database-side selection over the reference collection."""
        async with self._lock:
            keys = sorted(k for s, k in self._records if s == scope and (after is None or k > after))
            items = []
            for candidate in keys:
                record = self._live(scope, candidate)
                if _matches(record, key, query):
                    items.append(record)
                if len(items) > limit:
                    break
            return MemoryPage(tuple(items[:limit]), items[limit - 1].key if len(items) > limit else None)

    async def _delete(self, scope: MemoryScope, key: str, expected: str | None) -> bool:
        """Delete only the live revision selected inside the mutation lock."""
        async with self._lock:
            current = self._live(scope, key)
            revision_check(expected, current)
            return self._records.pop((scope, key), None) is not None

    async def _delete_all(self, scope: MemoryScope) -> int:
        """Remove expired and live records only from the requested namespace."""
        return await self._erase(lambda owner: owner == scope)

    async def _delete_user(self, scope: MemoryScope) -> int:
        """Erase all namespaces without changing any other application or user."""
        return await self._erase(lambda owner: (owner.application_id, owner.user_id) == (scope.application_id, scope.user_id))

    async def _erase(self, matches) -> int:
        """Serialize physical erasure with writes in the reference provider."""
        async with self._lock:
            identities = [identity for identity in self._records if matches(identity[0])]
            for identity in identities:
                del self._records[identity]
            return len(identities)

    async def _purge_expired(self, scope: MemoryScope, limit: int, after: str | None) -> MemoryCleanupPage:
        """Inspect a bounded ordered page and physically remove expired records."""
        async with self._lock:
            keys = sorted(key for owner, key in self._records if owner == scope and (after is None or key > after))
            deleted = sum(self._live(scope, key) is None for key in keys[:limit])
            return MemoryCleanupPage(deleted, keys[limit - 1] if len(keys) > limit else None)


def _matches(record: MemoryRecord | None, key: str | None, query: str | None) -> bool:
    """Keep reference filtering identical to the public literal-search contract."""
    return record is not None and (key is None or key == record.key) and (query is None or query in record.content)
