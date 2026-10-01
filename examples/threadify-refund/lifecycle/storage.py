"""Development storage for the refund example."""

from harnest import lifecycle
from harnest.store import MemoryStore


@lifecycle.storage.sessions
@lifecycle.storage.checkpoints
def state_store() -> MemoryStore:
    """Share one local store for sessions and approval checkpoints."""
    return MemoryStore()
