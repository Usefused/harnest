from harnest.lib.storage import store
from harnest.lifecycle import lifecycle


@lifecycle.session_store
def session_store():
    return store


@lifecycle.checkpointer
def checkpointer():
    return store
