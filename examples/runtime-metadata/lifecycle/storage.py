"""Bind one lifecycle-owned authority for sessions and checkpoints."""

from harnest.lib.storage import store
from harnest.lifecycle import lifecycle


@lifecycle.session_store
def session_store():
    """Provide the demo's in-memory session store."""

    return store


@lifecycle.checkpointer
def checkpointer():
    """Use the same store for private in-progress checkpoints."""

    return store
