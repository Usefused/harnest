"""Install the same lifecycle-owned storage for both runtime adapters."""
from harnest import lifecycle
from harnest.lib.storage import store


@lifecycle.storage.sessions
@lifecycle.storage.checkpoints
def storage():
    """Own and close the demo's in-memory sessions and checkpoints together."""
    return store
