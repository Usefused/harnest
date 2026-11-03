"""Redis-backed, user-scoped byte cache for a named custom storage role."""

from __future__ import annotations

import hashlib
import json
import os

from redis.asyncio import Redis

from harnest import context


class RedisCache:
    """Own one Redis pool and apply the deployment's ACL prefix to every key."""

    def __init__(self, url: str, *, application: str, prefix: str = "") -> None:
        """Namespace cache entries even when multiple applications share the same database."""
        if not application:
            raise ValueError("cache application must be nonempty")
        self._application = application
        self._prefix = prefix
        self._client = Redis.from_url(url, decode_responses=False)

    @classmethod
    def from_environment(cls, *, application: str) -> RedisCache:
        """Consume Hostlier URL and optional ACL prefix bindings at lifecycle construction."""
        return cls(os.environ["CACHE_URL"], application=application, prefix=os.getenv("CACHE_KEY_PREFIX", ""))

    async def start(self) -> None:
        """Check availability before admitting invocations."""
        await self._client.execute_command("PING")

    async def close(self) -> None:
        """Release pooled connections at lifecycle shutdown."""
        await self._client.aclose()

    def _key(self, key: str) -> str:
        """Bind entries to the managed invocation owner without revealing user identifiers in Redis."""
        scope = json.dumps([self._application, context.current().user_id, key], ensure_ascii=True)
        return self._prefix + "harnest-cache:" + hashlib.sha256(scope.encode()).hexdigest()

    async def get(self, key: str) -> bytes | None:
        """Return the cached bytes or a miss after expiry."""
        return await self._client.get(self._key(key))

    async def set(self, key: str, value: bytes, *, ttl_seconds: int) -> None:
        """Set a value and its mandatory expiration in one Redis operation."""
        if type(ttl_seconds) is not int or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer")
        await self._client.set(self._key(key), value, ex=ttl_seconds)

    async def delete(self, key: str) -> None:
        """Invalidate one entry within the current user's namespace."""
        await self._client.delete(self._key(key))
