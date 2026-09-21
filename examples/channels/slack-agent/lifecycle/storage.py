"""Retain scoped agent sessions and checkpoints across server restarts."""

from harnest import lifecycle
from harnest.lib.storage import store


@lifecycle.storage.sessions
def session_store():
    """Use the configured PostgreSQL store for authenticated actor sessions."""
    return store


@lifecycle.storage.checkpoints
def checkpoint_store():
    """Keep approval checkpoints in the same lifecycle-owned store."""
    return store
