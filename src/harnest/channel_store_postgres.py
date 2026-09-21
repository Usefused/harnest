"""PostgreSQL implementation of the portable channel persistence contract."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
import json
from typing import Any

from .channel_storage import ChannelEvent, ChannelReply, ChannelStoreConflictError
from .logging import get_logger
from .store_postgres import _create_pool
from .channel_store_postgres_schema import (
    ADMIT_EVENT_SQL,
    BIND_SESSION_SQL,
    ENQUEUE_REPLY_SQL,
    FINISH_REPLY_SQL,
    SCHEMA_SQL,
)


_AUDIT = get_logger("store.audit")
_SCHEMA_LOCK = 489_867_841_435_466_309


class PostgresChannelStore:
    """Persist channel events, session bindings, and replies with atomic admission.

    ``_pool`` supports composing this provider with other durable storage;
    externally supplied pools remain owned by their caller.
    """

    def __init__(
        self, dsn: str, *, pool_options: Mapping[str, Any] | None = None,
        setup_schema: bool = True, _pool: Any = None,
    ) -> None:
        """Retain connection settings without importing the optional driver."""

        self._dsn = dsn
        self._pool_options = dict(pool_options or {})
        self._setup_schema = setup_schema
        self._pool = _pool
        self._owns_pool = _pool is None

    async def start(self) -> None:
        """Open one pool and install or validate the durable schema."""

        if self._pool is None:
            self._pool = await _create_pool(self._dsn, self._pool_options)
        async with self._connection() as connection:
            async with connection.transaction():
                if self._setup_schema:
                    await connection.execute("SELECT pg_advisory_xact_lock($1)", _SCHEMA_LOCK)
                    await connection.execute(SCHEMA_SQL)
                else:
                    # An explicit projection detects incomplete provisioned schemas
                    # without silently creating database objects in restricted mode.
                    await connection.fetch("SELECT reply_to FROM harnest_channel_events LIMIT 0")
                    await connection.fetch("SELECT session_id FROM harnest_channel_sessions LIMIT 0")
                    await connection.fetch("SELECT status FROM harnest_channel_replies LIMIT 0")

    async def close(self) -> None:
        """Close only the pool opened by this instance."""

        pool, self._pool = self._pool, None
        if pool is not None and self._owns_pool:
            await pool.close()

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Any]:
        """Reject unstarted use before acquiring a pooled connection."""

        if self._pool is None:
            raise RuntimeError("PostgresChannelStore.start() must be called first")
        async with self._pool.acquire() as connection:
            yield connection

    async def admit_event(self, event: ChannelEvent) -> tuple[ChannelEvent, bool]:
        """Persist a new event or return an identical prior admission."""

        async with _mutation("channel.admit"):
            async with self._connection() as connection:
                async with connection.transaction():
                    row = await connection.fetchrow(ADMIT_EVENT_SQL, *_event_values(event))
                    if row is not None:
                        return _event(row), True
                    existing = await connection.fetchrow(
                        "SELECT * FROM harnest_channel_events WHERE platform=$1 "
                        "AND installation_id=$2 AND provider_event_id=$3",
                        event.platform, event.installation_id, event.provider_event_id,
                    )
                    current = _event(existing)
                    if current != event:
                        raise ChannelStoreConflictError(
                            "channel event identity has a different definition"
                        )
                    return current, False

    async def get_event(
        self, *, platform: str, installation_id: str, provider_event_id: str
    ) -> ChannelEvent | None:
        """Read one admitted event by its deduplication identity."""

        async with self._connection() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM harnest_channel_events WHERE platform=$1 "
                "AND installation_id=$2 AND provider_event_id=$3",
                platform, installation_id, provider_event_id,
            )
        return None if row is None else _event(row)

    async def bind_session(
        self, *, platform: str, installation_id: str, conversation_id: str,
        thread_id: str | None, session_id: str,
    ) -> None:
        """Record the session owning a conversation scope without overwriting it."""

        async with _mutation("channel.bind_session"):
            async with self._connection() as connection:
                await connection.execute(
                    BIND_SESSION_SQL, platform, installation_id, conversation_id,
                    thread_id or "", session_id,
                )

    async def get_session_binding(
        self, *, platform: str, installation_id: str, conversation_id: str,
        thread_id: str | None,
    ) -> str | None:
        """Read the session bound to a conversation scope, if any."""

        async with self._connection() as connection:
            return await connection.fetchval(
                "SELECT session_id FROM harnest_channel_sessions WHERE platform=$1 "
                "AND installation_id=$2 AND conversation_id=$3 AND thread_id=$4",
                platform, installation_id, conversation_id, thread_id or "",
            )

    async def enqueue_reply(self, reply: ChannelReply) -> ChannelReply:
        """Queue one reply, replacing any earlier reply with the same identity."""

        async with _mutation("channel.enqueue_reply"):
            async with self._connection() as connection:
                row = await connection.fetchrow(ENQUEUE_REPLY_SQL, *_reply_values(reply))
        return _reply(row)

    async def claim_replies(
        self, *, platform: str, installation_id: str, limit: int = 1
    ) -> tuple[ChannelReply, ...]:
        """Return a bounded scoped batch of pending replies, oldest first."""

        _require_limit(limit)
        async with self._connection() as connection:
            rows = await connection.fetch(
                "SELECT * FROM harnest_channel_replies WHERE platform=$1 "
                "AND installation_id=$2 AND status='pending' "
                "ORDER BY created_at, reply_id LIMIT $3",
                platform, installation_id, limit,
            )
        return tuple(_reply(row) for row in rows)

    async def finish_reply(
        self, *, reply_id: str, status: str, receipt: Mapping[str, Any] | None = None
    ) -> bool:
        """Record a terminal outcome for one previously enqueued reply."""

        _require_status(status)
        async with _mutation("channel.finish_reply"):
            async with self._connection() as connection:
                row = await connection.fetchrow(
                    FINISH_REPLY_SQL, reply_id, status, _dump_optional(receipt),
                )
        return row is not None


def _event_values(event: ChannelEvent) -> tuple[Any, ...]:
    """Encode private JSON only at the driver boundary."""

    return (
        event.platform, event.installation_id, event.provider_event_id, event.delivery_id,
        event.kind, event.sender_id, event.conversation_id, event.thread_id,
        event.message_id, event.occurred_at, event.content, _dump(event.reply_to),
    )


def _reply_values(reply: ChannelReply) -> tuple[Any, ...]:
    """Keep the persisted reply shape independent of runtime classes."""

    return (
        reply.reply_id, reply.platform, reply.installation_id, reply.conversation_id,
        _dump(reply.reply_to), reply.content, reply.status, _dump_optional(reply.receipt),
        reply.created_at, reply.updated_at,
    )


def _event(row: Mapping[str, Any]) -> ChannelEvent:
    """Decode a projected SQL row back into the public record shape."""

    values = dict(row)
    values["reply_to"] = _decode(values["reply_to"])
    return ChannelEvent(**values)


def _reply(row: Mapping[str, Any]) -> ChannelReply:
    """Decode a projected SQL row back into the public record shape."""

    values = dict(row)
    values["reply_to"] = _decode(values["reply_to"])
    values["receipt"] = _decode(values["receipt"])
    return ChannelReply(**values)


def _dump(value: Any) -> str:
    """Serialize immutable mapping values using the public JSON contract."""

    return json.dumps(dict(value), allow_nan=False)


def _dump_optional(value: Any) -> str | None:
    """Preserve SQL null separately from encoded values."""

    return None if value is None else _dump(value)


def _decode(value: Any) -> Any:
    """Allow default asyncpg JSON text and application-configured JSON codecs."""

    return json.loads(value) if isinstance(value, str) else value


def _require_limit(limit: int) -> None:
    """Bound every provider page before it reaches the database."""

    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000")


def _require_status(status: str) -> None:
    """Reject outcomes outside the durable reply status contract."""

    if status not in {"sent", "failed", "delivery_unknown"}:
        raise ValueError("finish status must be sent, failed, or delivery_unknown")


@asynccontextmanager
async def _mutation(operation: str) -> AsyncIterator[None]:
    """Emit privacy-safe outcomes after the surrounding transaction commits."""

    try:
        yield
    except Exception:
        _audit(operation, "failed")
        raise
    _audit(operation, "committed")


def _audit(operation: str, outcome: str) -> None:
    """Keep database payloads and exception text outside audit telemetry."""

    _AUDIT.info(operation, operation=operation, outcome=outcome, backend="postgres")


__all__ = ["PostgresChannelStore"]
