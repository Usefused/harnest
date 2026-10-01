"""Keep each isolated proposal session in process-local memory."""

from harnest import lifecycle
from harnest.store import MemoryStore


@lifecycle.storage.sessions
@lifecycle.storage.checkpoints
def state_store():
    """Share transient storage and let the Studio delete each completed proposal session."""
    return MemoryStore()
