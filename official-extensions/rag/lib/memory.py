"""Deterministic in-memory backend for development and contract tests."""

from __future__ import annotations

from collections.abc import Sequence
import math
import re

from .contracts import RAGChunk, RAGHit, RAGQuery, SearchMode


_WORD = re.compile(r"[A-Za-z0-9_]+")


class MemoryBackend:
    """Store a bounded corpus in process behind the standard backend contract."""

    def __init__(self, *, max_chunks: int = 10_000) -> None:
        """Set a hard capacity so development use cannot grow without bound."""

        if not isinstance(max_chunks, int) or isinstance(max_chunks, bool):
            raise TypeError("memory backend max_chunks must be an integer")
        if max_chunks < 1:
            raise ValueError("memory backend max_chunks must be positive")
        self._max_chunks = max_chunks
        self._chunks: dict[tuple[str, str], RAGChunk] = {}
        self._started = False

    async def start(self) -> None:
        """Activate the backend without allocating external resources."""

        self._started = True

    async def close(self) -> None:
        """Deactivate the backend while retaining its development corpus."""

        self._started = False

    async def replace(
        self,
        namespace: str,
        document_ids: Sequence[str],
        chunks: Sequence[RAGChunk],
    ) -> None:
        """Replace complete documents without exceeding configured capacity."""

        self._require_started()
        selected = set(document_ids)
        if any(
            chunk.namespace != namespace or chunk.document_id not in selected
            for chunk in chunks
        ):
            raise ValueError("replacement chunks must match the requested documents")
        retained = {
            key: chunk
            for key, chunk in self._chunks.items()
            if chunk.namespace != namespace or chunk.document_id not in selected
        }
        incoming = {(chunk.namespace, chunk.id): chunk for chunk in chunks}
        if len(incoming) != len(chunks):
            raise ValueError("replacement chunks must have unique identities")
        if len(retained) + len(incoming) > self._max_chunks:
            raise RuntimeError("memory RAG backend capacity exceeded")
        self._chunks = {**retained, **incoming}

    async def delete(self, namespace: str, document_ids: Sequence[str]) -> None:
        """Delete matching document chunks inside one namespace."""

        self._require_started()
        selected = set(document_ids)
        keys = [
            key
            for key, chunk in self._chunks.items()
            if chunk.namespace == namespace and chunk.document_id in selected
        ]
        for key in keys:
            del self._chunks[key]

    async def fetch(
        self, namespace: str, chunk_ids: Sequence[str]
    ) -> tuple[RAGChunk, ...]:
        """Return exact chunks in caller order and omit missing identities."""

        self._require_started()
        return tuple(
            chunk
            for chunk_id in chunk_ids
            if (chunk := self._chunks.get((namespace, chunk_id))) is not None
        )

    async def search(self, query: RAGQuery) -> tuple[RAGHit, ...]:
        """Filter, score, sort, and limit the development corpus in memory."""

        self._require_started()
        candidates = (
            chunk
            for chunk in self._chunks.values()
            if chunk.namespace == query.namespace and _matches(chunk, query)
        )
        hits = [RAGHit(chunk, _score(chunk, query)) for chunk in candidates]
        threshold = float("-inf") if query.min_score is None else query.min_score
        selected = [hit for hit in hits if hit.score >= threshold]
        selected.sort(key=lambda hit: (-hit.score, hit.chunk.id))
        return tuple(selected[: query.limit])

    def _require_started(self) -> None:
        """Match external backends by rejecting calls outside lifecycle."""

        if not self._started:
            raise RuntimeError("memory RAG backend is not started")


def _matches(chunk: RAGChunk, query: RAGQuery) -> bool:
    """Apply every portable metadata predicate before relevance scoring."""

    for name, expected in query.filters.items():
        actual = chunk.metadata.get(name)
        if isinstance(expected, tuple):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


def _score(chunk: RAGChunk, query: RAGQuery) -> float:
    """Combine normalized lexical and cosine scores for deterministic hybrid use."""

    keyword = _keyword_score(query.text, chunk)
    if query.mode is SearchMode.KEYWORD:
        return keyword
    semantic = _cosine(query.vector or (), chunk.embedding or ())
    if query.mode is SearchMode.SEMANTIC:
        return semantic
    return (keyword + semantic) / 2.0


def _keyword_score(text: str, chunk: RAGChunk) -> float:
    """Measure bounded query-term coverage across title and passage text."""

    query_words = set(_tokens(text))
    if not query_words:
        return 0.0
    corpus_words = set(_tokens(f"{chunk.title or ''} {chunk.text}"))
    return len(query_words & corpus_words) / len(query_words)


def _tokens(text: str) -> tuple[str, ...]:
    """Return lowercase word tokens for the dependency-free development scorer."""

    return tuple(match.group(0).lower() for match in _WORD.finditer(text))


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Return cosine similarity while treating absent vectors as no match."""

    if not left or len(left) != len(right):
        return 0.0
    magnitude = math.sqrt(sum(item * item for item in left)) * math.sqrt(
        sum(item * item for item in right)
    )
    if magnitude == 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / magnitude


__all__ = ["MemoryBackend"]
