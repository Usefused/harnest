"""Backend-neutral chunking, embedding, indexing, and retrieval orchestration."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import hashlib
from typing import Literal

from harnest.extensions import extension_mutation

from .contracts import (
    Chunker,
    Embedder,
    FilterValue,
    RAGBackend,
    RAGChunk,
    RAGDocument,
    RAGHit,
    RAGQuery,
    SearchMode,
    normalize_vectors,
    validate_backend,
    validate_identity,
)


MutationTrigger = Literal["agent", "user"]
NamespaceSource = str | Callable[[], str]
_MAX_INGEST_CHARS = 50_000_000
_MAX_INGEST_CHUNKS = 10_000


class FixedSizeChunker:
    """Split text on nearby whitespace with deterministic overlap."""

    def __init__(self, *, size: int = 2_000, overlap: int = 200) -> None:
        """Validate character bounds while remaining tokenizer-independent."""

        if not isinstance(size, int) or isinstance(size, bool) or size < 128:
            raise ValueError("chunk size must be an integer of at least 128")
        if not isinstance(overlap, int) or isinstance(overlap, bool):
            raise TypeError("chunk overlap must be an integer")
        if not 0 <= overlap < size:
            raise ValueError("chunk overlap must be non-negative and smaller than size")
        self._size = size
        self._overlap = overlap

    def split(self, document: RAGDocument) -> tuple[str, ...]:
        """Return stable non-empty passages without dropping source text."""

        text = document.text.strip()
        chunks: list[str] = []
        start = 0
        while start < len(text):
            stop = min(start + self._size, len(text))
            stop = self._boundary(text, start, stop)
            chunk = text[start:stop].strip()
            if chunk:
                if len(chunks) >= _MAX_INGEST_CHUNKS:
                    raise ValueError(
                        f"one document may create at most {_MAX_INGEST_CHUNKS} chunks"
                    )
                chunks.append(chunk)
            if stop >= len(text):
                break
            start = max(stop - self._overlap, start + 1)
        return tuple(chunks)

    @staticmethod
    def _boundary(text: str, start: int, stop: int) -> int:
        """Prefer a natural boundary without producing a severely short chunk."""

        if stop >= len(text):
            return stop
        floor = start + ((stop - start) // 2)
        candidates = [text.rfind(marker, floor, stop) for marker in ("\n\n", "\n", " ")]
        boundary = max(candidates)
        return stop if boundary < floor else boundary + 1


class RAGService:
    """Own one backend and expose the same RAG API to Tools and Tasks."""

    def __init__(
        self,
        backend: RAGBackend,
        *,
        embedder: Embedder | None = None,
        chunker: Chunker | None = None,
        namespace: NamespaceSource = "default",
        batch_size: int = 100,
    ) -> None:
        """Bind replaceable policies without opening external connections."""

        _validate_service_configuration(embedder, chunker, namespace, batch_size)
        self._backend = validate_backend(backend)
        self._embedder = embedder
        self._chunker = FixedSizeChunker() if chunker is None else chunker
        self._namespace_source = namespace
        self._batch_size = batch_size
        self._started = False

    @property
    def backend(self) -> RAGBackend:
        """Expose the configured adapter for provider-specific administration."""

        return self._backend

    async def start(self) -> None:
        """Open the backend once under application lifecycle ownership."""

        if self._started:
            return
        await self._backend.start()
        self._started = True

    async def close(self) -> None:
        """Close the backend once and make later calls fail closed."""

        if not self._started:
            return
        self._started = False
        await self._backend.close()

    async def __aenter__(self) -> "RAGService":
        """Allow direct use as a Harnest lifecycle resource."""

        await self.start()
        return self

    async def __aexit__(self, *_error: object) -> None:
        """Release the backend when its lifecycle resource exits."""

        await self.close()

    async def ingest(
        self,
        documents: Sequence[RAGDocument],
        *,
        trigger: MutationTrigger = "user",
    ) -> int:
        """Chunk, optionally embed, and atomically audit one ingestion request."""

        self._require_started()
        normalized = _documents(documents)
        namespace = self._namespace()
        chunks = self._chunks(normalized, namespace)
        embedded: list[RAGChunk] = []
        for offset in range(0, len(chunks), self._batch_size):
            batch = chunks[offset : offset + self._batch_size]
            embedded.extend(await self._embed_chunks(batch))
        document_ids = tuple(document.id for document in normalized)
        async with extension_mutation("rag", "documents.replace", trigger=trigger):
            await self._backend.replace(namespace, document_ids, embedded)
        return len(chunks)

    async def delete(
        self,
        document_ids: Sequence[str],
        *,
        trigger: MutationTrigger = "user",
    ) -> None:
        """Delete namespaced documents through the audited mutation boundary."""

        self._require_started()
        identities = _identities(document_ids, "document ids")
        async with extension_mutation("rag", "documents.delete", trigger=trigger):
            await self._backend.delete(self._namespace(), identities)

    async def fetch(self, chunk_ids: Sequence[str]) -> tuple[RAGChunk, ...]:
        """Fetch exact namespaced chunks in backend-defined source order."""

        self._require_started()
        identities = _identities(chunk_ids, "chunk ids")
        namespace = self._namespace()
        result = await self._backend.fetch(namespace, identities)
        return _chunks_result(result, namespace, identities)

    async def search(
        self,
        text: str,
        *,
        mode: SearchMode = SearchMode.HYBRID,
        limit: int = 8,
        filters: Mapping[str, FilterValue] | None = None,
        min_score: float | None = None,
    ) -> tuple[RAGHit, ...]:
        """Embed when required and delegate bounded ranking to the backend."""

        self._require_started()
        vector = await self._query_vector(text, mode)
        query = RAGQuery(
            text=text,
            namespace=self._namespace(),
            mode=mode,
            limit=limit,
            filters={} if filters is None else filters,
            vector=vector,
            min_score=min_score,
        )
        return _hits(await self._backend.search(query), query)

    def _chunks(
        self, documents: Sequence[RAGDocument], namespace: str
    ) -> tuple[RAGChunk, ...]:
        """Attach stable identities and namespace before storage sees passages."""

        chunks: list[RAGChunk] = []
        for document in documents:
            passages = _passages(
                self._chunker.split(document), available=_MAX_INGEST_CHUNKS - len(chunks)
            )
            for index, passage in enumerate(passages):
                chunks.append(
                    RAGChunk(
                        id=_chunk_id(document.id, index),
                        document_id=document.id,
                        namespace=namespace,
                        text=passage,
                        title=document.title,
                        uri=document.uri,
                        metadata=document.metadata,
                    )
                )
        return tuple(chunks)

    async def _embed_chunks(self, chunks: Sequence[RAGChunk]) -> tuple[RAGChunk, ...]:
        """Add embeddings while retaining the backend-neutral chunk contract."""

        if self._embedder is None or not chunks:
            return tuple(chunks)
        vectors = await self._embedder.embed(tuple(chunk.text for chunk in chunks))
        normalized = normalize_vectors(vectors, len(chunks))
        return tuple(_with_embedding(chunk, vector) for chunk, vector in zip(chunks, normalized))

    async def _query_vector(
        self, text: str, mode: SearchMode
    ) -> tuple[float, ...] | None:
        """Create exactly one query vector for semantic retrieval modes."""

        if mode is SearchMode.KEYWORD:
            return None
        if self._embedder is None:
            raise RuntimeError("semantic and hybrid search require an embedder")
        vectors = await self._embedder.embed((text,))
        return normalize_vectors(vectors, 1)[0]

    def _namespace(self) -> str:
        """Resolve tenant scope at call time so invocations cannot leak bindings."""

        value = self._namespace_source
        resolved = value() if callable(value) else value
        return validate_identity(resolved, "RAG namespace")

    def _require_started(self) -> None:
        """Reject accidental database access outside application lifecycle."""

        if not self._started:
            raise RuntimeError("RAG service is not started")


def _with_embedding(chunk: RAGChunk, vector: tuple[float, ...]) -> RAGChunk:
    """Copy one frozen chunk with its provider-independent embedding."""

    return RAGChunk(
        id=chunk.id,
        document_id=chunk.document_id,
        namespace=chunk.namespace,
        text=chunk.text,
        title=chunk.title,
        uri=chunk.uri,
        metadata=chunk.metadata,
        embedding=vector,
    )


def _documents(values: Sequence[RAGDocument]) -> tuple[RAGDocument, ...]:
    """Normalize a bounded materialized ingestion request."""

    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("documents must be a sequence")
    normalized = tuple(values)
    if not normalized:
        raise ValueError("documents cannot be empty")
    if len(normalized) > 1_000:
        raise ValueError("one ingestion request may contain at most 1000 documents")
    _validate_documents(normalized)
    return normalized


def _validate_documents(values: Sequence[RAGDocument]) -> None:
    """Validate document membership, identities, and aggregate payload size."""

    if any(not isinstance(item, RAGDocument) for item in values):
        raise TypeError("documents must contain only RAGDocument values")
    if len({item.id for item in values}) != len(values):
        raise ValueError("documents must have unique ids")
    if sum(len(item.text) for item in values) > _MAX_INGEST_CHARS:
        raise ValueError(
            f"one ingestion request may contain at most {_MAX_INGEST_CHARS} characters"
        )


def _passages(values: Sequence[str], *, available: int) -> tuple[str, ...]:
    """Materialize and bound one custom chunker's output before indexing."""

    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("chunker must return a sequence of strings")
    if len(values) > available:
        raise ValueError(
            f"one ingestion request may create at most {_MAX_INGEST_CHUNKS} chunks"
        )
    passages = tuple(values)
    if not passages or any(
        not isinstance(item, str) or not item.strip() for item in passages
    ):
        raise ValueError("chunker must return one or more non-empty strings")
    return passages


def _chunk_id(document_id: str, index: int) -> str:
    """Keep readable stable IDs when bounded and hash only oversized sources."""

    candidate = f"{document_id}:{index}"
    if len(candidate) <= 256:
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    return f"chunk:{digest}"


def _identities(values: Sequence[str], label: str) -> tuple[str, ...]:
    """Reject empty or duplicate exact-identity operations."""

    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{label} must be a sequence")
    normalized = tuple(values)
    if not normalized or any(not isinstance(item, str) or not item.strip() for item in normalized):
        raise ValueError(f"{label} must contain non-empty strings")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{label} must be unique")
    for item in normalized:
        validate_identity(item, label)
    return normalized


def _chunks_result(
    values: Sequence[RAGChunk], namespace: str, requested: Sequence[str]
) -> tuple[RAGChunk, ...]:
    """Validate custom backend fetch output and tenant isolation."""

    result = tuple(values)
    if any(not isinstance(item, RAGChunk) for item in result):
        raise TypeError("RAG backend fetch must return only RAGChunk values")
    if len(result) > len(requested):
        raise ValueError("RAG backend returned more chunks than requested")
    if any(item.namespace != namespace for item in result):
        raise ValueError("RAG backend returned a chunk from another namespace")
    if any(item.id not in requested for item in result):
        raise ValueError("RAG backend returned an unrequested chunk")
    if len({item.id for item in result}) != len(result):
        raise ValueError("RAG backend returned duplicate chunks")
    return result


def _hits(values: Sequence[RAGHit], query: RAGQuery) -> tuple[RAGHit, ...]:
    """Enforce namespace, score, and result bounds around custom adapters."""

    result = tuple(values)
    _validate_hit_types(result)
    _validate_hit_bounds(result, query)
    return result


def _validate_service_configuration(
    embedder: Embedder | None,
    chunker: Chunker | None,
    namespace: NamespaceSource,
    batch_size: int,
) -> None:
    """Validate replaceable policies separately from service ownership."""

    if embedder is not None and not isinstance(embedder, Embedder):
        raise TypeError("RAG embedder must implement embed")
    if chunker is not None and not isinstance(chunker, Chunker):
        raise TypeError("RAG chunker must implement split")
    if not isinstance(namespace, str) and not callable(namespace):
        raise TypeError("RAG namespace must be text or a callable")
    if not isinstance(batch_size, int) or isinstance(batch_size, bool):
        raise TypeError("RAG batch size must be an integer")
    if not 1 <= batch_size <= 1_000:
        raise ValueError("RAG batch size must be between 1 and 1000")


def _validate_hit_types(result: Sequence[RAGHit]) -> None:
    """Reject custom backend values outside the portable hit contract."""

    if any(not isinstance(item, RAGHit) for item in result):
        raise TypeError("RAG backend search must return only RAGHit values")


def _validate_hit_bounds(result: Sequence[RAGHit], query: RAGQuery) -> None:
    """Enforce tenancy, threshold, ordering, and requested result count."""

    if len(result) > query.limit:
        raise ValueError("RAG backend returned more hits than requested")
    if any(item.chunk.namespace != query.namespace for item in result):
        raise ValueError("RAG backend returned a chunk from another namespace")
    if query.min_score is not None and any(item.score < query.min_score for item in result):
        raise ValueError("RAG backend returned a hit below the requested minimum score")
    if any(left.score < right.score for left, right in zip(result, result[1:])):
        raise ValueError("RAG backend must return hits in descending score order")


__all__ = ["FixedSizeChunker", "MutationTrigger", "NamespaceSource", "RAGService"]
