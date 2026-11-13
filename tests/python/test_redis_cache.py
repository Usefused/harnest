"""Exercise Hostlier's Redis bindings through the managed invocation storage API."""

import os
import unittest
from unittest.mock import patch

from fakeredis.aioredis import FakeRedis

from harnest import context
from harnest.context import activate_context, create_agent_context
from harnest_redis.cache import RedisCache


class RedisCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_uses_acl_prefix_ttl_and_authenticated_owner(self):
        client = FakeRedis()
        with patch.dict(os.environ, {"CACHE_URL": "redis://example", "CACHE_KEY_PREFIX": "granted:"}), \
             patch("harnest_redis.cache.Redis.from_url", return_value=client):
            cache = RedisCache.from_environment(application="support")
        await cache.start()
        self.addAsyncCleanup(cache.close)
        with self.scope(cache, "alice"):
            await context.storage("answers", RedisCache).set("same", b"answer", ttl_seconds=60)
            self.assertEqual(await cache.get("same"), b"answer")
            [key] = await client.keys("*")
            self.assertTrue(key.startswith(b"granted:"))
            self.assertGreater(await client.ttl(key), 0)
        with self.scope(cache, "bob"):
            self.assertIsNone(await cache.get("same"))
            await cache.delete("same")
        with self.scope(cache, "alice"):
            self.assertEqual(await cache.get("same"), b"answer")
            await cache.delete("same")
            self.assertIsNone(await cache.get("same"))
            with self.assertRaises(ValueError):
                await cache.set("x", b"", ttl_seconds=0)

    def scope(self, cache, user):
        """Bind the real custom store under an independent invocation owner."""
        return activate_context(create_agent_context(framework="adk", agent_name="support",
            invocation_id="invoke", user_id=user, session_id="session", metadata={},
            resources={}, custom_stores={"answers": cache}))
