"""Verify PostgreSQL RAG queries with fakes and an optional local database."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
import os
import uuid

import pytest

from harnest_extension_rag.extension import (
    PostgresBackend,
    RAGChunk,
    RAGDocument,
    RAGQuery,
    SearchMode,
    rag,
)


class _AsyncContext:
    """Expose one fake asyncpg connection or transaction as a context."""

    def __init__(self, value) -> None:
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_error):
        return None


class Connection:
    """Capture parameterized SQL without interpreting datastore behavior."""

    def __init__(self) -> None:
        self.executed = []
        self.batches = []
        self.records = []
        self.transactions = 0

    def transaction(self):
        """Record that document replacement uses one transaction."""

        self.transactions += 1
        return _AsyncContext(self)

    async def execute(self, query, *parameters):
        """Capture schema and mutation statements."""

        self.executed.append((query, parameters))

    async def executemany(self, query, rows):
        """Capture the bounded replacement batch."""

        self.batches.append((query, tuple(rows)))

    async def fetch(self, query, *parameters):
        """Return the configured datastore-limited result page."""

        self.executed.append((query, parameters))
        return self.records

    async def fetchrow(self, query, *parameters):
        """Accept operator-managed schema validation."""

        self.executed.append((query, parameters))
        return None


class Pool:
    """Borrow one fake connection through asyncpg's acquisition shape."""

    def __init__(self, connection) -> None:
        self.connection = connection

    def acquire(self):
        """Return the reusable connection context."""

        return _AsyncContext(self.connection)


def _record() -> dict[str, object]:
    """Return one portable row in the PostgreSQL projection shape."""

    return {
        "namespace": "tenant",
        "chunk_id": "doc:0",
        "document_id": "doc",
        "text": "graph retrieval",
        "title": "Guide",
        "uri": "https://example.test/guide",
        "metadata": '{"kind":"guide"}',
        "embedding": [1.0, 0.0],
        "score": 0.9,
    }


def test_postgres_backend_owns_schema_replacement_and_queries() -> None:
    """Keep uniqueness, filtering, scoring, ordering, and limiting in SQL."""

    async def exercise():
        connection = Connection()
        backend = PostgresBackend(pool=Pool(connection))
        await backend.start()
        chunk = RAGChunk(
            "doc:0",
            "doc",
            "tenant",
            "graph retrieval",
            metadata={"kind": "guide"},
            embedding=(1.0, 0.0),
        )
        await backend.replace("tenant", ("doc",), (chunk,))
        connection.records = [_record()]
        hits = await backend.search(
            RAGQuery(
                "graph",
                "tenant",
                mode=SearchMode.HYBRID,
                vector=(1.0, 0.0),
                filters={"kind": "guide"},
            )
        )
        fetched = await backend.fetch("tenant", ("doc:0",))
        await backend.close()
        return connection, hits, fetched

    connection, hits, fetched = asyncio.run(exercise())
    schema = connection.executed[0][0]
    search, parameters = connection.executed[-2]
    assert "PRIMARY KEY (namespace, chunk_id)" in schema
    assert "USING GIN (search_vector)" in schema
    assert connection.transactions == 1
    assert "ON CONFLICT (namespace, chunk_id)" in connection.batches[0][0]
    assert "chunk.metadata @> $6::jsonb" in search
    assert "ORDER BY ranked.score DESC" in search
    assert parameters[5] == '{"kind":"guide"}'
    assert hits[0].chunk == fetched[0]


@pytest.mark.parametrize("mode", tuple(SearchMode))
def test_postgres_selects_one_datastore_scoring_mode(mode: SearchMode) -> None:
    """Generate bounded native SQL for keyword, semantic, and hybrid search."""

    async def exercise() -> str:
        connection = Connection()
        connection.records = []
        backend = PostgresBackend(pool=Pool(connection), setup_schema=False)
        await backend.start()
        vector = None if mode is SearchMode.KEYWORD else (1.0, 0.0)
        await backend.search(RAGQuery("query", "tenant", mode=mode, vector=vector))
        return connection.executed[-1][0]

    sql = asyncio.run(exercise())
    assert "LIMIT (SELECT result_limit FROM request)" in sql
    assert "chunk.namespace=request.namespace" in sql


_POSTGRES_DSN = os.environ.get("HARNEST_TEST_POSTGRES_DSN")


@pytest.mark.skipif(not _POSTGRES_DSN, reason="set HARNEST_TEST_POSTGRES_DSN")
def test_postgres_live_replaces_and_retrieves_documents() -> None:
    """Exercise schema, transactions, full text, and vector SQL on PostgreSQL."""

    class Embedder:
        """Produce deterministic vectors for the live database test."""

        async def embed(
            self, texts: Sequence[str]
        ) -> tuple[tuple[float, float], ...]:
            return tuple(
                (float("marigold" in text.casefold()), 1.0) for text in texts
            )

    class Sections:
        """Split pipe-delimited fixture sections into stable chunks."""

        def split(self, document: RAGDocument) -> tuple[str, ...]:
            return tuple(document.text.split("|"))

    async def exercise() -> None:
        table = f"rag_test_{uuid.uuid4().hex[:12]}"
        service = rag.postgres(
            _POSTGRES_DSN,
            table=table,
            embedder=Embedder(),
            chunker=Sections(),
            namespace="tenant-live",
        )
        try:
            async with service:
                await service.ingest(
                    (
                        RAGDocument(
                            "manual",
                            "Marigold launch phrase|obsolete",
                            metadata={"kind": "manual"},
                        ),
                    )
                )
                assert len(await service.fetch(("manual:0", "manual:1"))) == 2
                await service.ingest(
                    (
                        RAGDocument(
                            "manual",
                            "Marigold launch phrase",
                            metadata={"kind": "manual"},
                        ),
                    )
                )
                assert await service.fetch(("manual:1",)) == ()
                for mode in SearchMode:
                    hits = await service.search(
                        "Marigold", mode=mode, filters={"kind": "manual"}, limit=1
                    )
                    assert hits[0].chunk.document_id == "manual"
        finally:
            import asyncpg

            connection = await asyncpg.connect(_POSTGRES_DSN)
            try:
                await connection.execute(f'DROP TABLE IF EXISTS "{table}"')
            finally:
                await connection.close()

    asyncio.run(exercise())
