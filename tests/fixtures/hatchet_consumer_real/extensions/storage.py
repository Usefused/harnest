"""Live PostgreSQL ownership for both sessions and framework checkpoints."""

from __future__ import annotations

import os

from harnest.lifecycle import lifecycle
from harnest.store import PostgresStore


def _dsn() -> str:
    """Require the private DSN without placing it in source or diagnostics."""

    value = os.environ.get("HARNEST_HATCHET_POSTGRES_URL")
    if not value:
        raise RuntimeError("HARNEST_HATCHET_POSTGRES_URL is required")
    return value


_STORE = PostgresStore(_dsn())


@lifecycle.storage.sessions
def sessions() -> PostgresStore:
    """Reuse one pool for durable session and transcript ownership."""

    return _STORE


@lifecycle.storage.checkpoints
def checkpoints() -> PostgresStore:
    """Reuse the same transaction-capable store for external continuations."""

    return _STORE
