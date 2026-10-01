"""PostgreSQL storage and datastore-side ranking for the RAG extension."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
import importlib
import json
import re
from typing import Any, AsyncIterator

from .contracts import RAGChunk, RAGHit, RAGQuery, SearchMode


_TABLE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")


class PostgresBackend:
    """Persist chunks in PostgreSQL with full-text and array-vector ranking."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        table: str = "harnest_rag_chunks",
        pool: Any | None = None,
        setup_schema: bool = True,
        pool_options: Mapping[str, Any] | None = None,
    ) -> None:
        """Retain validated connection settings without opening a connection."""

        if pool is None and (not isinstance(dsn, str) or not dsn.strip()):
            raise ValueError("PostgreSQL DSN is required when pool is omitted")
        if not isinstance(table, str) or _TABLE_NAME.fullmatch(table) is None:
            raise ValueError("PostgreSQL RAG table must be a safe identifier")
        if not isinstance(setup_schema, bool):
            raise TypeError("PostgreSQL setup_schema must be boolean")
        if pool_options is not None and not isinstance(pool_options, Mapping):
            raise TypeError("PostgreSQL pool_options must be a mapping")
        self._dsn = dsn
        self._table = table
        self._pool = pool
        self._setup_schema = setup_schema
        self._pool_options = {} if pool_options is None else dict(pool_options)
        self._owned_pool = False
        self._started = False

    async def start(self) -> None:
        """Open an owned pool and ensure the isolated RAG schema is usable."""

        if self._started:
            return
        if self._pool is None:
            self._pool = await _create_pool(self._dsn, self._pool_options)
            self._owned_pool = True
        self._started = True
        try:
            if self._setup_schema:
                await self._ensure_schema()
            else:
                await self._validate_schema()
        except BaseException:
            self._started = False
            await self._close_owned_pool()
            raise

    async def close(self) -> None:
        """Close only a pool created by this backend."""

        self._started = False
        await self._close_owned_pool()

    async def replace(
        self,
        namespace: str,
        document_ids: Sequence[str],
        chunks: Sequence[RAGChunk],
    ) -> None:
        """Replace complete documents atomically so obsolete chunks cannot survive."""

        table = _quoted(self._table)
        rows = tuple(_chunk_row(chunk) for chunk in chunks)
        async with self._connection() as connection:
            async with connection.transaction():
                await connection.execute(
                    f"DELETE FROM {table} WHERE namespace=$1 AND document_id=ANY($2::text[])",
                    namespace,
                    list(document_ids),
                )
                if rows:
                    await connection.executemany(_insert_sql(table), rows)

    async def delete(self, namespace: str, document_ids: Sequence[str]) -> None:
        """Delete complete documents inside one namespace."""

        if not document_ids:
            return
        async with self._connection() as connection:
            await connection.execute(
                f"DELETE FROM {_quoted(self._table)} "
                "WHERE namespace=$1 AND document_id=ANY($2::text[])",
                namespace,
                list(document_ids),
            )

    async def fetch(
        self, namespace: str, chunk_ids: Sequence[str]
    ) -> tuple[RAGChunk, ...]:
        """Fetch exact chunks in caller order with ordering performed by PostgreSQL."""

        if not chunk_ids:
            return ()
        query = f"""
        SELECT {_projection("chunk")}
        FROM unnest($2::text[]) WITH ORDINALITY AS requested(chunk_id, position)
        JOIN {_quoted(self._table)} AS chunk
          ON chunk.namespace=$1 AND chunk.chunk_id=requested.chunk_id
        ORDER BY requested.position ASC
        """
        async with self._connection() as connection:
            records = await connection.fetch(query, namespace, list(chunk_ids))
        return tuple(_chunk(record) for record in records)

    async def search(self, query: RAGQuery) -> tuple[RAGHit, ...]:
        """Filter, rank, order, and limit one query entirely in PostgreSQL."""

        sql, parameters = _search_sql(self._table, query)
        async with self._connection() as connection:
            records = await connection.fetch(sql, *parameters)
        return tuple(
            RAGHit(chunk=_chunk(record), score=float(record["score"]))
            for record in records
        )

    async def _ensure_schema(self) -> None:
        """Create an extension-owned table and its two portable indexes."""

        table = _quoted(self._table)
        document_index = _quoted(f"{self._table}_document_idx")
        search_index = _quoted(f"{self._table}_search_idx")
        statement = f"""
        CREATE TABLE IF NOT EXISTS {table} (
            namespace TEXT NOT NULL,
            chunk_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            text TEXT NOT NULL,
            title TEXT,
            uri TEXT,
            metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            embedding DOUBLE PRECISION[],
            search_vector TSVECTOR GENERATED ALWAYS AS (
                setweight(to_tsvector('simple', coalesce(title, '')), 'A') ||
                setweight(to_tsvector('simple', text), 'B')
            ) STORED,
            PRIMARY KEY (namespace, chunk_id)
        );
        CREATE INDEX IF NOT EXISTS {document_index}
            ON {table} (namespace, document_id);
        CREATE INDEX IF NOT EXISTS {search_index}
            ON {table} USING GIN (search_vector);
        """
        async with self._connection() as connection:
            await connection.execute(statement)

    async def _validate_schema(self) -> None:
        """Fail startup before traffic when operator-managed schema is incompatible."""

        async with self._connection() as connection:
            await connection.fetchrow(
                f"SELECT {_projection('chunk')}, chunk.search_vector "
                f"FROM {_quoted(self._table)} AS chunk LIMIT 0"
            )

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Any]:
        """Centralize lifecycle enforcement and pool acquisition."""

        if not self._started or self._pool is None:
            raise RuntimeError("PostgreSQL RAG backend is not started")
        async with self._pool.acquire() as connection:
            yield connection

    async def _close_owned_pool(self) -> None:
        """Release an internally created pool after stop or failed startup."""

        pool = self._pool
        if self._owned_pool and pool is not None:
            await pool.close()
            self._pool = None
            self._owned_pool = False


async def _create_pool(dsn: str | None, options: Mapping[str, Any]) -> Any:
    """Import asyncpg only when the PostgreSQL backend starts."""

    try:
        asyncpg = importlib.import_module("asyncpg")
    except ModuleNotFoundError as error:  # pragma: no cover - dependency contract
        raise RuntimeError("PostgreSQL RAG requires the asyncpg package") from error
    return await asyncpg.create_pool(dsn=dsn, **dict(options))


def _insert_sql(table: str) -> str:
    """Return one conflict-safe insert used inside document replacement."""

    return f"""
    INSERT INTO {table} (
        namespace, chunk_id, document_id, text, title, uri, metadata, embedding
    ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::double precision[])
    ON CONFLICT (namespace, chunk_id) DO UPDATE SET
        document_id=EXCLUDED.document_id,
        text=EXCLUDED.text,
        title=EXCLUDED.title,
        uri=EXCLUDED.uri,
        metadata=EXCLUDED.metadata,
        embedding=EXCLUDED.embedding
    """


def _search_sql(table: str, query: RAGQuery) -> tuple[str, tuple[Any, ...]]:
    """Build a parameterized query without accepting caller-authored SQL."""

    filters, filter_parameters = _filter_sql(query.filters, first_parameter=6)
    score = _score_sql(query.mode)
    availability = _availability_sql(query.mode)
    sql = f"""
    WITH request AS (
        SELECT
            $1::text AS namespace,
            $2::text AS query_text,
            $3::double precision[] AS query_vector,
            $4::integer AS result_limit,
            $5::double precision AS min_score
    ), ranked AS (
        SELECT {_projection('chunk')}, {score} AS score
        FROM {_quoted(table)} AS chunk
        CROSS JOIN request
        WHERE chunk.namespace=request.namespace
          AND ({availability})
          {filters}
    )
    SELECT ranked.*
    FROM ranked
    CROSS JOIN request
    WHERE request.min_score IS NULL OR ranked.score >= request.min_score
    ORDER BY ranked.score DESC, ranked.chunk_id ASC
    LIMIT (SELECT result_limit FROM request)
    """
    parameters: tuple[Any, ...] = (
        query.namespace,
        query.text,
        None if query.vector is None else list(query.vector),
        query.limit,
        query.min_score,
        *filter_parameters,
    )
    return sql, parameters


def _score_sql(mode: SearchMode) -> str:
    """Select lexical, cosine, or balanced hybrid scoring inside PostgreSQL."""

    keyword = "ts_rank_cd(chunk.search_vector, plainto_tsquery('simple', request.query_text))"
    semantic = _cosine_sql()
    if mode is SearchMode.KEYWORD:
        return keyword
    if mode is SearchMode.SEMANTIC:
        return semantic
    return f"(0.5 * {keyword}) + (0.5 * GREATEST({semantic}, 0.0))"


def _availability_sql(mode: SearchMode) -> str:
    """Exclude rows that cannot participate in the selected retrieval mode."""

    keyword = "chunk.search_vector @@ plainto_tsquery('simple', request.query_text)"
    vector = (
        "chunk.embedding IS NOT NULL AND "
        "cardinality(chunk.embedding)=cardinality(request.query_vector)"
    )
    if mode is SearchMode.KEYWORD:
        return keyword
    if mode is SearchMode.SEMANTIC:
        return vector
    return f"({keyword}) OR ({vector})"


def _cosine_sql() -> str:
    """Return a zero-safe cosine expression over native float arrays."""

    return """
    COALESCE((
        SELECT SUM(pair.left_value * pair.right_value) /
               NULLIF(
                   SQRT(SUM(pair.left_value * pair.left_value)) *
                   SQRT(SUM(pair.right_value * pair.right_value)),
                   0.0
               )
        FROM unnest(chunk.embedding, request.query_vector)
             AS pair(left_value, right_value)
    ), 0.0)
    """


def _filter_sql(
    filters: Mapping[str, object], *, first_parameter: int
) -> tuple[str, tuple[str, ...]]:
    """Translate portable scalar membership filters to JSONB containment."""

    clauses: list[str] = []
    parameters: list[str] = []
    position = first_parameter
    for name, expected in filters.items():
        values = expected if isinstance(expected, tuple) else (expected,)
        alternatives = []
        for value in values:
            alternatives.append(f"chunk.metadata @> ${position}::jsonb")
            parameters.append(json.dumps({name: value}, separators=(",", ":")))
            position += 1
        clauses.append("(" + " OR ".join(alternatives) + ")")
    if not clauses:
        return "", ()
    return "AND " + " AND ".join(clauses), tuple(parameters)


def _projection(variable: str) -> str:
    """Return the fixed portable projection shared by fetch and search."""

    return (
        f"{variable}.namespace, {variable}.chunk_id, {variable}.document_id, "
        f"{variable}.text, {variable}.title, {variable}.uri, "
        f"{variable}.metadata, {variable}.embedding"
    )


def _chunk_row(chunk: RAGChunk) -> tuple[Any, ...]:
    """Project one immutable chunk into asyncpg parameters."""

    return (
        chunk.namespace,
        chunk.id,
        chunk.document_id,
        chunk.text,
        chunk.title,
        chunk.uri,
        json.dumps(dict(chunk.metadata), sort_keys=True, separators=(",", ":")),
        None if chunk.embedding is None else list(chunk.embedding),
    )


def _chunk(source: Mapping[str, Any]) -> RAGChunk:
    """Reconstruct a validated portable chunk from an asyncpg record."""

    raw_metadata = source.get("metadata") or {}
    metadata = json.loads(raw_metadata) if isinstance(raw_metadata, str) else raw_metadata
    embedding = source.get("embedding")
    return RAGChunk(
        id=source["chunk_id"],
        document_id=source["document_id"],
        namespace=source["namespace"],
        text=source["text"],
        title=source.get("title"),
        uri=source.get("uri"),
        metadata=metadata,
        embedding=None if embedding is None else tuple(embedding),
    )


def _quoted(value: str) -> str:
    """Quote a previously validated table or index identifier."""

    if _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("PostgreSQL RAG identifier must be safe")
    return f'"{value}"'


__all__ = ["PostgresBackend"]
