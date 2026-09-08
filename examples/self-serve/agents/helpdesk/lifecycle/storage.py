from harnest.lib.storage import store
from harnest import lifecycle


@lifecycle.storage.sessions
def session_store():
    return store


@lifecycle.storage.checkpoints
def checkpointer():
    return store
