"""Bind the example's shared in-memory storage to the managed runtime."""

from harnest import lifecycle
from harnest.lib.storage import store


@lifecycle.storage.sessions
def session_store():
    """Use process-local sessions for this development example."""
    return store


@lifecycle.storage.checkpoints
def checkpointer():
    """Keep checkpoints under the same lifecycle as committed sessions."""
    return store
