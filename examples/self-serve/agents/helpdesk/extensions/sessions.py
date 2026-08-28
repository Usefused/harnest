from harnest.lifecycle import lifecycle
from harnest.session import InMemorySessionStore


@lifecycle.session_store
def session_store():
    return InMemorySessionStore()
