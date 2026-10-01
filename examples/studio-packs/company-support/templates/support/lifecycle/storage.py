"""Share one local development store across session and checkpoint lifecycles."""
from harnest import lifecycle
from harnest.store import MemoryStore

store = MemoryStore()


@lifecycle.storage.sessions
def session_store():
    """Keep sessions available between local preview turns."""
    return store


@lifecycle.storage.checkpoints
def checkpointer():
    """Use the same authority for private execution checkpoints."""
    return store
