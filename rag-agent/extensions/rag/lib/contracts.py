"""Provider-neutral documents, retrieval requests, and backend contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
import math
import re
from typing import Protocol, runtime_checkable


_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~:/-]{0,255}$")
_FILTER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_MAX_CHUNK_CHARS = 1_000_000
_MAX_DOCUMENT_CHARS = 10_000_000
_MAX_EMBEDDING_DIMENSIONS = 65_536
_MAX_METADATA_ITEMS = 64
_MAX_METADATA_TEXT_CHARS = 32_768
_MAX_QUERY_CHARS = 16_384
Scalar = str | int | float | bool
FilterValue = Scalar | tuple[Scalar, ...]


class SearchMode(str, Enum):
    """Choose lexical, vector, or rank-fused retrieval."""

    KEYWORD = "keyword"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"


@dataclass(frozen=True, slots=True)
class RAGDocument:
    """One source document supplied to the chunking and indexing pipeline."""

    id: str
    text: str
    title: str | None = None
    uri: str | None = None
    metadata: Mapping[str, Scalar] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Freeze validated source metadata before adapters persist it."""

        _require_identity(self.id, "document id")
        _require_text(self.text, "document text", maximum=_MAX_DOCUMENT_CHARS)
        _optional_text(self.title, "document title", maximum=8_192)
        _optional_text(self.uri, "document URI", maximum=8_192)
        object.__setattr__(self, "metadata", _metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class RAGChunk:
    """One independently retrievable, namespace-scoped document passage."""

    id: str
    document_id: str
    namespace: str
    text: str
    title: str | None = None
    uri: str | None = None
    metadata: Mapping[str, Scalar] = field(default_factory=dict)
    embedding: tuple[float, ...] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Normalize immutable fields shared by every storage adapter."""

        _require_identity(self.id, "chunk id")
        _require_identity(self.document_id, "document id")
        _require_identity(self.namespace, "namespace")
        _require_text(self.text, "chunk text", maximum=_MAX_CHUNK_CHARS)
        _optional_text(self.title, "chunk title", maximum=8_192)
        _optional_text(self.uri, "chunk URI", maximum=8_192)
        object.__setattr__(self, "metadata", _metadata(self.metadata))
        object.__setattr__(self, "embedding", _embedding(self.embedding))


@dataclass(frozen=True, slots=True)
class RAGQuery:
    """A bounded retrieval request understood by every backend."""

    text: str
    namespace: str
    mode: SearchMode = SearchMode.HYBRID
    limit: int = 8
    filters: Mapping[str, FilterValue] = field(default_factory=dict)
    vector: tuple[float, ...] | None = field(default=None, repr=False)
    min_score: float | None = None

    def __post_init__(self) -> None:
        """Reject ambiguous or unbounded queries at the shared boundary."""

        _require_text(self.text, "query text", maximum=_MAX_QUERY_CHARS)
        _require_identity(self.namespace, "namespace")
        if not isinstance(self.mode, SearchMode):
            raise TypeError("query mode must be a SearchMode")
        if not isinstance(self.limit, int) or isinstance(self.limit, bool):
            raise TypeError("query limit must be an integer")
        if not 1 <= self.limit <= 100:
            raise ValueError("query limit must be between 1 and 100")
        normalized = _filters(self.filters)
        object.__setattr__(self, "filters", normalized)
        vector = _embedding(self.vector)
        if self.mode is not SearchMode.KEYWORD and vector is None:
            raise ValueError("semantic and hybrid queries require a vector")
        object.__setattr__(self, "vector", vector)
        if self.min_score is not None:
            _finite_number(self.min_score, "minimum score")
            object.__setattr__(self, "min_score", float(self.min_score))


@dataclass(frozen=True, slots=True)
class RAGHit:
    """One ranked passage returned from a RAG backend."""

    chunk: RAGChunk
    score: float

    def __post_init__(self) -> None:
        """Keep provider scores finite before exposing them to agent code."""

        if not isinstance(self.chunk, RAGChunk):
            raise TypeError("RAG hit chunk must be a RAGChunk")
        _finite_number(self.score, "RAG hit score")
        object.__setattr__(self, "score", float(self.score))


@runtime_checkable
class RAGBackend(Protocol):
    """Storage adapter required by the provider-neutral RAG service."""

    async def start(self) -> None:
        """Open backend resources and validate required indexes."""

    async def close(self) -> None:
        """Release only resources owned by this backend."""

    async def replace(
        self,
        namespace: str,
        document_ids: Sequence[str],
        chunks: Sequence[RAGChunk],
    ) -> None:
        """Atomically replace complete documents and remove obsolete chunks."""

    async def delete(self, namespace: str, document_ids: Sequence[str]) -> None:
        """Delete documents only inside the supplied namespace."""

    async def fetch(self, namespace: str, chunk_ids: Sequence[str]) -> Sequence[RAGChunk]:
        """Fetch exact chunks without performing relevance ranking."""

    async def search(self, query: RAGQuery) -> Sequence[RAGHit]:
        """Apply filtering, ranking, ordering, and limiting in the backend."""


@runtime_checkable
class Embedder(Protocol):
    """Turn text into vectors without coupling RAG to a model provider."""

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """Return one finite vector for every supplied text."""


@runtime_checkable
class Chunker(Protocol):
    """Split one document into ordered text passages."""

    def split(self, document: RAGDocument) -> Sequence[str]:
        """Return non-empty chunks in source order."""


def validate_backend(backend: object) -> RAGBackend:
    """Fail early when a custom backend omits part of the shared contract."""

    if not isinstance(backend, RAGBackend):
        raise TypeError(
            "RAG backend must implement start, close, replace, delete, fetch, and search"
        )
    return backend


def normalize_vectors(
    vectors: Sequence[Sequence[float]], expected: int
) -> tuple[tuple[float, ...], ...]:
    """Validate embedder cardinality and return immutable finite vectors."""

    if len(vectors) != expected:
        raise ValueError("embedder must return one vector for every input text")
    normalized = tuple(_embedding(vector) for vector in vectors)
    if any(vector is None for vector in normalized):
        raise ValueError("embedder vectors cannot be empty")
    return tuple(vector for vector in normalized if vector is not None)


def validate_filter_name(name: str) -> str:
    """Return a backend-safe metadata key shared by query adapters."""

    if not isinstance(name, str) or _FILTER_NAME.fullmatch(name) is None:
        raise ValueError(f"invalid RAG filter name {name!r}")
    return name


def validate_identity(value: str, label: str) -> str:
    """Return one validated namespace or document identity."""

    _require_identity(value, label)
    return value


def _require_identity(value: str, label: str) -> None:
    """Validate bounded identities before using them in indexes or audit fields."""

    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ValueError(f"{label} must be a safe non-empty identifier")


def _require_text(value: str, label: str, *, maximum: int | None = None) -> None:
    """Require meaningful text within a provider-independent safety bound."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty")
    if maximum is not None and len(value) > maximum:
        raise ValueError(f"{label} may contain at most {maximum} characters")


def _optional_text(
    value: str | None, label: str, *, maximum: int | None = None
) -> None:
    """Reject empty optional values so adapters see one canonical absence."""

    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"{label} must be non-empty when supplied")
    if value is not None and maximum is not None and len(value) > maximum:
        raise ValueError(f"{label} may contain at most {maximum} characters")


def _metadata(value: Mapping[str, Scalar]) -> Mapping[str, Scalar]:
    """Freeze metadata after validating portable scalar values."""

    if not isinstance(value, Mapping):
        raise TypeError("RAG metadata must be a mapping")
    if len(value) > _MAX_METADATA_ITEMS:
        raise ValueError(f"RAG metadata may contain at most {_MAX_METADATA_ITEMS} fields")
    normalized: dict[str, Scalar] = {}
    for name, item in value.items():
        validate_filter_name(name)
        if not isinstance(item, (str, int, float, bool)):
            raise TypeError("RAG metadata values must be strings, numbers, or booleans")
        if isinstance(item, str) and len(item) > _MAX_METADATA_TEXT_CHARS:
            raise ValueError("RAG metadata text is too long")
        if isinstance(item, float):
            _finite_number(item, "RAG metadata number")
        normalized[name] = item
    return MappingProxyType(normalized)


def _filters(value: Mapping[str, FilterValue]) -> Mapping[str, FilterValue]:
    """Freeze scalar or set-valued filters without accepting raw query DSL."""

    if not isinstance(value, Mapping):
        raise TypeError("RAG filters must be a mapping")
    if len(value) > _MAX_METADATA_ITEMS:
        raise ValueError(f"RAG filters may contain at most {_MAX_METADATA_ITEMS} fields")
    normalized: dict[str, FilterValue] = {}
    for name, item in value.items():
        validate_filter_name(name)
        _validate_filter_values(item)
        normalized[name] = item
    return MappingProxyType(normalized)


def _validate_filter_values(value: FilterValue) -> None:
    """Bound one scalar or membership filter and validate every member."""

    values = value if isinstance(value, tuple) else (value,)
    if len(values) > 100:
        raise ValueError("one RAG membership filter may contain at most 100 values")
    if not values or any(
        not isinstance(entry, (str, int, float, bool)) for entry in values
    ):
        raise TypeError("RAG filters must contain portable scalar values")
    for entry in values:
        if isinstance(entry, str) and len(entry) > _MAX_METADATA_TEXT_CHARS:
            raise ValueError("RAG filter text is too long")
        if isinstance(entry, float):
            _finite_number(entry, "RAG filter number")


def _embedding(value: Sequence[float] | None) -> tuple[float, ...] | None:
    """Normalize vectors and reject boolean, empty, or non-finite dimensions."""

    if value is None:
        return None
    if isinstance(value, (str, bytes)):
        raise TypeError("embedding must be a sequence of numbers")
    if len(value) > _MAX_EMBEDDING_DIMENSIONS:
        raise ValueError(
            f"embedding may contain at most {_MAX_EMBEDDING_DIMENSIONS} dimensions"
        )
    normalized = tuple(value)
    if not normalized:
        raise ValueError("embedding cannot be empty")
    for item in normalized:
        if not isinstance(item, (int, float)) or isinstance(item, bool):
            raise TypeError("embedding values must be numbers")
        _finite_number(item, "embedding value")
    return tuple(float(item) for item in normalized)


def _finite_number(value: float, label: str) -> None:
    """Reject NaN and infinity before values cross a datastore boundary."""

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{label} must be a number")
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")


__all__ = [
    "Chunker",
    "Embedder",
    "FilterValue",
    "RAGBackend",
    "RAGChunk",
    "RAGDocument",
    "RAGHit",
    "RAGQuery",
    "Scalar",
    "SearchMode",
    "normalize_vectors",
    "validate_backend",
    "validate_filter_name",
    "validate_identity",
]
