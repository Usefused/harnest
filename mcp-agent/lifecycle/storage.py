from harnest import lifecycle
from harnest.store import MemoryStore


@lifecycle.storage.sessions
@lifecycle.storage.checkpoints
def state_store():
    """Share one lifecycle-owned store without placing it in lib."""
    return MemoryStore()
