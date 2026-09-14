"""Typed authoring surface for the official RAG Harnest Extension."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Protocol

from harnest.extensions import Extension

Scalar = str | int | float | bool
FilterValue = Scalar | tuple[Scalar, ...]
MutationTrigger = Literal["agent", "user"]
NamespaceSource = str | Callable[[], str]

class SearchMode(str, Enum):
    KEYWORD: SearchMode
    SEMANTIC: SearchMode
    HYBRID: SearchMode

@dataclass(frozen=True)
class RAGDocument:
    id: str
    text: str
    title: str | None = ...
    uri: str | None = ...
    metadata: Mapping[str, Scalar] = ...

@dataclass(frozen=True)
class RAGChunk:
    id: str
    document_id: str
    namespace: str
    text: str
    title: str | None = ...
    uri: str | None = ...
    metadata: Mapping[str, Scalar] = ...
    embedding: tuple[float, ...] | None = ...

@dataclass(frozen=True)
class RAGQuery:
    text: str
    namespace: str
    mode: SearchMode = ...
    limit: int = ...
    filters: Mapping[str, FilterValue] = ...
    vector: tuple[float, ...] | None = ...
    min_score: float | None = ...

@dataclass(frozen=True)
class RAGHit:
    chunk: RAGChunk
    score: float

class RAGBackend(Protocol):
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def replace(
        self,
        namespace: str,
        document_ids: Sequence[str],
        chunks: Sequence[RAGChunk],
    ) -> None: ...
    async def delete(self, namespace: str, document_ids: Sequence[str]) -> None: ...
    async def fetch(
        self, namespace: str, chunk_ids: Sequence[str]
    ) -> Sequence[RAGChunk]: ...
    async def search(self, query: RAGQuery) -> Sequence[RAGHit]: ...

class Embedder(Protocol):
    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...

class Chunker(Protocol):
    def split(self, document: RAGDocument) -> Sequence[str]: ...

class FixedSizeChunker:
    def __init__(self, *, size: int = ..., overlap: int = ...) -> None: ...
    def split(self, document: RAGDocument) -> tuple[str, ...]: ...

class MemoryBackend(RAGBackend):
    def __init__(self, *, max_chunks: int = ...) -> None: ...

class PostgresBackend(RAGBackend):
    def __init__(
        self,
        dsn: str | None = ...,
        *,
        table: str = ...,
        pool: Any | None = ...,
        setup_schema: bool = ...,
        pool_options: Mapping[str, Any] | None = ...,
    ) -> None: ...

class RAGService:
    backend: RAGBackend
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def __aenter__(self) -> RAGService: ...
    async def __aexit__(self, *_error: object) -> None: ...
    async def ingest(
        self,
        documents: Sequence[RAGDocument],
        *,
        trigger: MutationTrigger = ...,
    ) -> int: ...
    async def delete(
        self,
        document_ids: Sequence[str],
        *,
        trigger: MutationTrigger = ...,
    ) -> None: ...
    async def fetch(self, chunk_ids: Sequence[str]) -> tuple[RAGChunk, ...]: ...
    async def search(
        self,
        text: str,
        *,
        mode: SearchMode = ...,
        limit: int = ...,
        filters: Mapping[str, FilterValue] | None = ...,
        min_score: float | None = ...,
    ) -> tuple[RAGHit, ...]: ...

class RAGExtension(Extension):
    def service(
        self,
        backend: RAGBackend,
        *,
        embedder: Embedder | None = ...,
        chunker: Chunker | None = ...,
        namespace: NamespaceSource = ...,
        batch_size: int = ...,
    ) -> RAGService: ...
    def memory(
        self,
        *,
        embedder: Embedder | None = ...,
        chunker: Chunker | None = ...,
        namespace: NamespaceSource = ...,
        batch_size: int = ...,
        max_chunks: int = ...,
    ) -> RAGService: ...
    def postgres(
        self,
        dsn: str | None = ...,
        *,
        table: str = ...,
        pool: Any | None = ...,
        setup_schema: bool = ...,
        pool_options: Mapping[str, Any] | None = ...,
        embedder: Embedder | None = ...,
        chunker: Chunker | None = ...,
        namespace: NamespaceSource = ...,
        batch_size: int = ...,
    ) -> RAGService: ...

extension: RAGExtension
rag: RAGExtension
