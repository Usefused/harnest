from harnest import lifecycle
from harnest.lib.storage import store


@lifecycle.storage.sessions
@lifecycle.storage.checkpoints
def state_store():
    """Share the store passed to the native LangGraph checkpointer."""
    return store
