"""Verify provider-neutral RAG orchestration and custom backend support."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from harnest_extension_rag.extension import (
    RAGChunk,
    RAGDocument,
    RAGHit,
    RAGQuery,
    SearchMode,
    rag,
)


class Embedder:
    """Return deterministic two-dimensional vectors for test text."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, float], ...]:
        """Record calls and retain simple lexical evidence in each vector."""

        normalized = tuple(texts)
        self.calls.append(normalized)
        return tuple((float("elastic" in text.lower()), 1.0) for text in normalized)


def test_memory_service_ingests_searches_fetches_and_deletes() -> None:
    """Exercise the released RAG journey without external infrastructure."""

    async def exercise() -> None:
        embedder = Embedder()
        service = rag.memory(
            embedder=embedder,
            namespace="tenant-a",
            max_chunks=20,
        )
        documents = (
            RAGDocument("guide", "PostgreSQL supports hybrid retrieval.", metadata={"kind": "guide"}),
            RAGDocument("note", "RAG connects private knowledge.", metadata={"kind": "note"}),
        )

        async with service:
            assert await service.ingest(documents) == 2
            hits = await service.search(
                "PostgreSQL retrieval",
                filters={"kind": "guide"},
                limit=1,
            )
            assert [hit.chunk.document_id for hit in hits] == ["guide"]
            assert await service.fetch(("guide:0",)) == (hits[0].chunk,)
            await service.delete(("guide",))
            assert await service.fetch(("guide:0",)) == ()

        assert len(embedder.calls) == 2

    asyncio.run(exercise())


def test_service_accepts_a_custom_backend_contract() -> None:
    """Keep provider extensibility independent from built-in adapters."""

    class Backend:
        def __init__(self) -> None:
            self.query: RAGQuery | None = None

        async def start(self) -> None:
            """Open no resources for this custom adapter."""

        async def close(self) -> None:
            """Close no resources for this custom adapter."""

        async def replace(
            self,
            namespace: str,
            document_ids: Sequence[str],
            chunks: Sequence[RAGChunk],
        ) -> None:
            """Accept complete document replacement for protocol coverage."""

        async def delete(self, namespace: str, document_ids: Sequence[str]) -> None:
            """Accept namespaced deletion for protocol coverage."""

        async def fetch(self, namespace: str, chunk_ids: Sequence[str]) -> tuple[RAGChunk, ...]:
            """Return no exact matches for protocol coverage."""

            return ()

        async def search(self, query: RAGQuery) -> tuple[RAGHit, ...]:
            """Capture the normalized query produced by the shared service."""

            self.query = query
            return ()

    async def exercise() -> Backend:
        backend = Backend()
        service = rag.service(backend, namespace="tenant-b")
        async with service:
            assert await service.search("graph", mode=SearchMode.KEYWORD) == ()
        return backend

    backend = asyncio.run(exercise())
    assert backend.query is not None
    assert backend.query.namespace == "tenant-b"
    assert backend.query.mode is SearchMode.KEYWORD


def test_contracts_reject_cross_provider_ambiguity() -> None:
    """Reject unsafe bounds and vector values before adapters receive them."""

    with pytest.raises(ValueError, match="vector"):
        RAGQuery("query", "tenant", mode=SearchMode.SEMANTIC)
    with pytest.raises(ValueError, match="between"):
        RAGQuery("query", "tenant", mode=SearchMode.KEYWORD, limit=101)
    with pytest.raises(TypeError, match="metadata"):
        RAGDocument("document", "text", metadata={"nested": {"unsafe": True}})


def test_memory_reingest_removes_obsolete_chunks() -> None:
    """Apply the complete-document replacement contract in development too."""

    class Sections:
        """Split a compact fixture into explicit chunk boundaries."""

        def split(self, document: RAGDocument) -> tuple[str, ...]:
            return tuple(document.text.split("|"))

    async def exercise() -> None:
        service = rag.memory(chunker=Sections(), namespace="tenant")
        async with service:
            await service.ingest((RAGDocument("manual", "current|obsolete"),))
            await service.ingest((RAGDocument("manual", "current"),))
            assert await service.fetch(("manual:0", "manual:1")) == (
                RAGChunk("manual:0", "manual", "tenant", "current"),
            )

    asyncio.run(exercise())


def test_ingestion_rejects_duplicate_documents_and_unbounded_chunks() -> None:
    """Bound materialization before an embedder or datastore sees the request."""

    class TooManyChunks:
        """Return one more passage than the service-wide safety limit."""

        def split(self, document: RAGDocument) -> tuple[str, ...]:
            return ("x",) * 10_001

    async def duplicate_documents() -> None:
        service = rag.memory(namespace="tenant")
        async with service:
            with pytest.raises(ValueError, match="unique ids"):
                await service.ingest(
                    (RAGDocument("same", "one"), RAGDocument("same", "two"))
                )

    async def excessive_chunks() -> None:
        service = rag.memory(chunker=TooManyChunks(), namespace="tenant")
        async with service:
            with pytest.raises(ValueError, match="at most 10000 chunks"):
                await service.ingest((RAGDocument("large", "value"),))

    asyncio.run(duplicate_documents())
    asyncio.run(excessive_chunks())
