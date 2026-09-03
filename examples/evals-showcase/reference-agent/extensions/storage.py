from harnest.lifecycle import lifecycle
from harnest.store import MemoryStore


@lifecycle.storage.sessions
@lifecycle.storage.checkpoints
def state_store() -> MemoryStore:
    """Share one process-local store across sessions and checkpoints."""

    return MemoryStore()
