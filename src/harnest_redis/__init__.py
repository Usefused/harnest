"""Bundled Redis provider for Harnest's public storage contracts."""

from harnest.task_store_redis import RedisTaskStore as RedisStore

__all__ = ["RedisStore"]
