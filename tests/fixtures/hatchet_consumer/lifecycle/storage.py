"""Portable persistence used by the managed Hatchet consumer fixture."""

from harnest.checkpoint import MemoryStore
from harnest.lifecycle import lifecycle
from harnest.session import InMemorySessionStore


@lifecycle.storage.sessions
def sessions() -> InMemorySessionStore:
    """Keep the consumer transcript available across external job completion."""

    return InMemorySessionStore()


@lifecycle.storage.checkpoints
def checkpoints() -> MemoryStore:
    """Retain the exact managed graph position while the job is outstanding."""

    return MemoryStore()
