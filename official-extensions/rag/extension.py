"""Provider-neutral RAG factories for Harnest agents and durable Tasks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from harnest.extensions import Extension

from .lib.contracts import (
    Chunker,
    Embedder,
    FilterValue,
    RAGBackend,
    RAGChunk,
    RAGDocument,
    RAGHit,
    RAGQuery,
    Scalar,
    SearchMode,
)
from .lib.memory import MemoryBackend
from .lib.postgres import PostgresBackend
from .lib.service import FixedSizeChunker, NamespaceSource, RAGService


class RAGExtension(Extension):
    """Create lifecycle-owned RAG services without choosing a storage provider."""

    def service(
        self,
        backend: RAGBackend,
        *,
        embedder: Embedder | None = None,
        chunker: Chunker | None = None,
        namespace: NamespaceSource = "default",
        batch_size: int = 100,
    ) -> RAGService:
        """Wrap any compatible backend in the shared ingestion and retrieval API."""

        return RAGService(
            backend,
            embedder=embedder,
            chunker=chunker,
            namespace=namespace,
            batch_size=batch_size,
        )

    def memory(
        self,
        *,
        embedder: Embedder | None = None,
        chunker: Chunker | None = None,
        namespace: NamespaceSource = "default",
        batch_size: int = 100,
        max_chunks: int = 10_000,
    ) -> RAGService:
        """Create a bounded in-memory service for development and unit tests."""

        return self.service(
            MemoryBackend(max_chunks=max_chunks),
            embedder=embedder,
            chunker=chunker,
            namespace=namespace,
            batch_size=batch_size,
        )

    def postgres(
        self,
        dsn: str | None = None,
        *,
        table: str = "harnest_rag_chunks",
        pool: Any | None = None,
        setup_schema: bool = True,
        pool_options: Mapping[str, Any] | None = None,
        embedder: Embedder | None = None,
        chunker: Chunker | None = None,
        namespace: NamespaceSource = "default",
        batch_size: int = 100,
    ) -> RAGService:
        """Create a PostgreSQL-backed lexical, semantic, or hybrid service."""

        backend = PostgresBackend(
            dsn,
            table=table,
            pool=pool,
            setup_schema=setup_schema,
            pool_options=pool_options,
        )
        return self.service(
            backend,
            embedder=embedder,
            chunker=chunker,
            namespace=namespace,
            batch_size=batch_size,
        )

extension = RAGExtension()
rag = extension


__all__ = [
    "Chunker",
    "Embedder",
    "FilterValue",
    "FixedSizeChunker",
    "MemoryBackend",
    "PostgresBackend",
    "RAGBackend",
    "RAGChunk",
    "RAGDocument",
    "RAGExtension",
    "RAGHit",
    "RAGQuery",
    "RAGService",
    "Scalar",
    "SearchMode",
    "extension",
    "rag",
]
