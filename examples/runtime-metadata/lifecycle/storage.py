"""Bind one lifecycle-owned authority for sessions and checkpoints."""

from harnest.lib.storage import store
from harnest import lifecycle


@lifecycle.storage.sessions
def session_store():
    """Provide the demo's in-memory session store."""

    return store


@lifecycle.storage.checkpoints
def checkpointer():
    """Use the same store for private in-progress checkpoints."""

    return store
