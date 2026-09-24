"""Keep example conversations inside the agent process."""

from harnest import lifecycle
from harnest.store import MemoryStore


@lifecycle.storage.sessions
@lifecycle.storage.checkpoints
def state_store() -> MemoryStore:
    """Share process-local session and checkpoint storage."""
    return MemoryStore()
