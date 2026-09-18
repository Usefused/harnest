# Harnest RAG Extension

The official RAG Harnest Extension gives Agent Tools and Tasks one typed API for
retrieval-augmented generation. It includes durable PostgreSQL storage, a
bounded in-memory backend, document chunking and embedding orchestration,
tenant namespaces, metadata filters, exact fetches, and keyword, semantic, and
hybrid search. It also defines the provider contract that separate datastore
extensions can implement.

This is a Harnest Extension, not an Agent Plugin. It runs as trusted application
code under Harnest lifecycle management.

## Table of contents

- [What the extension provides](#what-the-extension-provides)
- [RAG and Harnest long-term memory](#rag-and-harnest-long-term-memory)
- [Install](#install)
- [Quick start](#quick-start)
- [Supply an embedder](#supply-an-embedder)
- [Index and replace documents](#index-and-replace-documents)
- [Search](#search)
- [Fetch exact chunks](#fetch-exact-chunks)
- [Delete documents](#delete-documents)
- [Use metadata filters](#use-metadata-filters)
- [Customize chunking](#customize-chunking)
- [Isolate users and tenants](#isolate-users-and-tenants)
- [Configure PostgreSQL](#configure-postgresql)
- [Use the memory backend](#use-the-memory-backend)
- [Build another backend](#build-another-backend)
- [Build an extension on RAG](#build-an-extension-on-rag)
- [Public API](#public-api)
- [Validation and safety bounds](#validation-and-safety-bounds)

Read the detailed public guide at
[docs.usefused.com/harnest/build/extensions/official/rag](https://docs.usefused.com/harnest/build/extensions/official/rag).

## What the extension provides

| Capability | Behavior |
| --- | --- |
| Document ingestion | Validates, chunks, optionally embeds, and atomically replaces complete documents. |
| Keyword search | Uses PostgreSQL full-text search and a generated GIN-indexed search vector. |
| Semantic search | Computes cosine similarity inside PostgreSQL over portable float arrays. |
| Hybrid search | Combines lexical rank and non-negative cosine similarity with equal weighting. |
| Exact fetch | Retrieves requested chunk IDs in caller order without relevance ranking. |
| Metadata filtering | Supports portable scalar equality and membership filters without accepting datastore query syntax. |
| Tenant isolation | Resolves a namespace for every operation and validates backend results before returning them. |
| Lifecycle ownership | Opens and closes owned resources through `RAGService`, including async context-manager use. |
| Audited mutations | Emits payload-free Harnest extension audit events for document replacement and deletion. |
| Provider extensibility | Exposes `RAGBackend`, `Embedder`, and `Chunker` protocols for other implementations. |

The extension does not choose an embedding model, expose retrieval as a Tool
automatically, or install a PostgreSQL server. Application code retains those
decisions.

## RAG and Harnest long-term memory

Use this extension for application knowledge: documents are chunked, optionally
embedded, filtered, and ranked for grounded retrieval. Use Harnest's built-in
`context.memory` for deliberate, user-scoped facts and preferences that Agent
Tools save and retrieve across sessions. Core memory performs literal text
search and does not automatically add records to a model prompt.

The similarly named `rag.memory(...)` factory is only the process-local test
backend for this RAG API; it is unrelated to `context.memory`. An agent may use
both systems: core memory for explicit user facts and RAG for a searchable
document corpus.

## Install

Requires Harnest `>=0.21.3,<0.24`.

Install the published extension into an agent project, then synchronize its
Python environment:

```bash
harnest extensions install rag --project my-agent
harnest env sync my-agent
```

The distribution is `harnest-extension-rag`. Its only datastore client
dependency is `asyncpg`; stock PostgreSQL is sufficient and `pgvector` is not
required.

## Quick start

Create one application lifecycle resource. The service can then be resolved by
trusted Tools and Tasks:

```python
# lifecycle/knowledge.py
import os

from harnest import context, lifecycle
from harnest.extensions.rag import RAGService, rag
from lib.embeddings import embeddings


knowledge = rag.postgres(
    os.environ["DATABASE_URL"],
    table="product_knowledge",
    embedder=embeddings,
    namespace=lambda: context.current().user_id,
)


@lifecycle.resource
@context.provider("knowledge")
async def knowledge_resource():
    async with knowledge:
        yield knowledge
```

The service must be started before use. An application lifecycle resource is
the normal ownership boundary; `async with service` is convenient for scripts
and tests.

## Supply an embedder

Semantic and hybrid search require an application-supplied `Embedder`. The
extension deliberately does not depend on a model vendor:

```python
from collections.abc import Sequence


class Embeddings:
    """Adapt the selected embedding client to the RAG contract."""

    def __init__(self, client) -> None:
        self._client = client

    async def embed(
        self, texts: Sequence[str]
    ) -> Sequence[Sequence[float]]:
        return await self._client.embed(texts)
```

The embedder must return exactly one non-empty, finite vector per input text.
All stored and query vectors used together must have the same dimensions.
Keyword-only search works without an embedder.

## Index and replace documents

Call `ingest` from trusted application or Task code:

```python
from harnest.extensions.rag import RAGDocument, RAGService


async def index_manual(knowledge: RAGService) -> int:
    return await knowledge.ingest(
        (
            RAGDocument(
                id="manual-2026",
                title="Product manual",
                uri="https://docs.example.com/manual",
                text="PostgreSQL-backed product knowledge...",
                metadata={"kind": "manual", "version": 2026},
            ),
        ),
        trigger="user",
    )
```

`ingest` returns the number of generated chunks. Every supplied document is a
complete replacement: the backend deletes its previous chunks and inserts the
new set in one transaction. Re-indexing a shorter document therefore removes
obsolete chunks. Duplicate document IDs in one request are rejected.

Use `trigger="user"` for user-initiated writes and `trigger="agent"` for
agent-initiated writes. The trigger is audit metadata; it does not replace Tool
permission or approval policy.

## Search

Resolve the service and expose only the fields the model needs:

```python
# tools/search_knowledge.py
from harnest import context
from harnest.agent import tool
from harnest.extensions.rag import RAGService, SearchMode


@tool
async def search_knowledge(query: str, limit: int = 6) -> list[dict]:
    """Find grounded passages in the caller's knowledge namespace."""

    knowledge = context.resource("knowledge", RAGService)
    hits = await knowledge.search(
        query,
        mode=SearchMode.HYBRID,
        filters={"kind": ("manual", "release-note")},
        limit=limit,
    )
    return [
        {
            "document_id": hit.chunk.document_id,
            "title": hit.chunk.title,
            "uri": hit.chunk.uri,
            "text": hit.chunk.text,
            "score": hit.score,
        }
        for hit in hits
    ]
```

`search` accepts:

- `text`: the non-empty search text.
- `mode`: `KEYWORD`, `SEMANTIC`, or `HYBRID`; the default is `HYBRID`.
- `limit`: between 1 and 100 results; the default is 8.
- `filters`: portable metadata equality or membership predicates.
- `min_score`: an optional finite minimum provider score.

The backend applies filtering, projection, ranking, ordering, and limiting in
the datastore. `RAGService` then validates result types, namespace isolation,
score ordering, threshold compliance, and count before exposing hits.

### Search modes

| Mode | Embedder required | PostgreSQL behavior |
| --- | --- | --- |
| `SearchMode.KEYWORD` | No | Matches the generated text-search vector and orders by `ts_rank_cd`. |
| `SearchMode.SEMANTIC` | Yes | Orders compatible stored vectors by cosine similarity. |
| `SearchMode.HYBRID` | Yes | Includes lexical or vector candidates and combines both scores with 0.5 weighting. |

Store stable document identity, title, and URI values so Tool results can retain
source attribution.

## Fetch exact chunks

Use `fetch` when the caller already knows chunk identities:

```python
chunks = await knowledge.fetch(("manual-2026:0", "manual-2026:3"))
```

PostgreSQL returns existing chunks in the requested order. Missing IDs are
omitted. Fetch is namespace-scoped and performs no ranking.

## Delete documents

Delete complete documents by their document IDs:

```python
await knowledge.delete(("manual-2025",), trigger="user")
```

Deletion cannot cross the service's active namespace. Like ingestion, it runs
inside the Harnest extension mutation audit boundary. Do not expose `ingest` or
`delete` directly to the model without explicit permission and approval policy.

## Use metadata filters

Metadata values are portable strings, integers, finite floats, or booleans.
Search filters accept one scalar for equality or a tuple for membership:

```python
hits = await knowledge.search(
    "upgrade authentication",
    filters={
        "kind": ("manual", "release-note"),
        "published": True,
        "major_version": 4,
    },
)
```

Different filter fields are combined with AND; tuple members within one field
are combined with OR. PostgreSQL translates them to parameterized JSONB
containment predicates. Nested objects and caller-authored SQL or datastore DSL
are not accepted.

## Customize chunking

The default `FixedSizeChunker(size=2000, overlap=200)` splits on nearby
whitespace where possible and creates stable chunk IDs from document ID and
source order. Supply a custom synchronous `Chunker` when the application needs
format-aware passages:

```python
from collections.abc import Sequence
from harnest.extensions.rag import RAGDocument


class MarkdownSections:
    def split(self, document: RAGDocument) -> Sequence[str]:
        return tuple(
            section.strip()
            for section in document.text.split("\n## ")
            if section.strip()
        )


knowledge = rag.postgres(
    os.environ["DATABASE_URL"],
    embedder=embeddings,
    chunker=MarkdownSections(),
)
```

A custom chunker must return a non-empty sequence of non-empty strings. The
shared ingestion bounds still apply.

## Isolate users and tenants

`namespace` may be a fixed safe identity or a zero-argument callable. Callable
namespaces are resolved for every operation, which lets one lifecycle-owned
service follow the current invocation:

```python
namespace=lambda: context.current().user_id
```

Namespace is part of PostgreSQL's chunk primary key and every query predicate.
The service also rejects any custom backend result belonging to another
namespace. Choose a stable tenant or access-domain identity; do not use a
display name that may change.

## Configure PostgreSQL

`rag.postgres(...)` accepts these storage and orchestration options:

| Option | Default | Purpose |
| --- | --- | --- |
| `dsn` | None | asyncpg DSN; required unless `pool` is supplied. |
| `table` | `harnest_rag_chunks` | Safe extension-owned table name. |
| `pool` | None | Borrow an existing asyncpg-compatible pool. |
| `setup_schema` | `True` | Create the table and indexes, or validate an operator-managed schema when false. |
| `pool_options` | None | Extra keyword options passed to `asyncpg.create_pool`. |
| `embedder` | None | Application embedding adapter. |
| `chunker` | `FixedSizeChunker()` | Application chunking policy. |
| `namespace` | `default` | Fixed or per-operation tenant namespace. |
| `batch_size` | 100 | Number of chunk texts sent to the embedder per call. |

When `pool` is supplied, the extension borrows it and never closes it. Otherwise
the service lazily imports asyncpg, creates the pool during `start`, and closes
it during `close`. Failed schema setup also closes an owned pool.

The default schema contains namespace and document identities, text, optional
title and URI, JSONB metadata, a portable `double precision[]` embedding, and a
stored generated text-search vector. It creates:

- a primary key on `(namespace, chunk_id)`;
- an index on `(namespace, document_id)`; and
- a GIN index on the generated search vector.

Set `setup_schema=False` only when operators provision the same table shape
before application startup. Table names are validated identifiers rather than
interpolated caller SQL.

## Use the memory backend

`rag.memory(...)` supports deterministic unit tests and small local
demonstrations:

```python
knowledge = rag.memory(
    embedder=embeddings,
    namespace="test-tenant",
    max_chunks=500,
)
```

It implements the same replacement, filtering, scoring, and result contracts,
but it is process-local, non-durable, and intentionally bounded. It is not a
production knowledge store.

## Build another backend

Wrap any compatible backend with `rag.service(...)`. A backend owns datastore
resources and implements the complete `RAGBackend` protocol:

```python
from collections.abc import Sequence
from harnest.extensions.rag import RAGChunk, RAGHit, RAGQuery


class CompanySearchBackend:
    async def start(self) -> None: ...
    async def close(self) -> None: ...

    async def replace(
        self,
        namespace: str,
        document_ids: Sequence[str],
        chunks: Sequence[RAGChunk],
    ) -> None: ...

    async def delete(
        self, namespace: str, document_ids: Sequence[str]
    ) -> None: ...

    async def fetch(
        self, namespace: str, chunk_ids: Sequence[str]
    ) -> Sequence[RAGChunk]: ...

    async def search(self, query: RAGQuery) -> Sequence[RAGHit]: ...


knowledge = rag.service(
    CompanySearchBackend(),
    embedder=embeddings,
    namespace="customer-a",
)
```

`replace` receives complete document identities and their new chunks. It must
atomically remove obsolete chunks and publish the replacement. `search` must
apply predicates, projection, ranking, descending score order, and
`query.limit` in the datastore rather than materializing an unbounded result
set in Python.

## Build an extension on RAG

Elasticsearch, Neo4j, or another provider should be a separate Harnest Extension
that depends on this base contract. Its manifest declares the dependency:

```yaml
# extensions/company-search/extension.yaml
apiVersion: harnest.dev/v1alpha1
kind: Extension
metadata:
  name: company_search
  version: 1.0.0
runtime:
  entrypoint: extension:extension
requires:
  extensions: [rag]
```

The provider extension imports the shared types from the public namespace:

```python
from harnest.extensions.rag import RAGBackend, RAGChunk, RAGQuery
```

Harnest rejects missing or cyclic dependencies, imports dependencies before
dependants, starts them in dependency order, and closes them in reverse order.
A published provider distribution should also declare
`harnest-extension-rag` as a Python package dependency so installation brings
the base distribution with it.

## Public API

| API | Role |
| --- | --- |
| `rag` / `extension` | Installed `RAGExtension` singleton. |
| `rag.postgres(...)` | Create the stock-PostgreSQL service. |
| `rag.memory(...)` | Create a bounded in-memory service. |
| `rag.service(...)` | Wrap a custom `RAGBackend`. |
| `RAGService` | Lifecycle-owned ingestion, deletion, fetch, and search API. |
| `RAGDocument` | Source document with optional title, URI, and scalar metadata. |
| `RAGChunk` | Immutable namespaced retrievable passage with optional embedding. |
| `RAGQuery` | Normalized backend query containing text, mode, filters, vector, limit, and threshold. |
| `RAGHit` | Ranked chunk and finite score. |
| `SearchMode` | Keyword, semantic, or hybrid retrieval selection. |
| `Embedder` | Async model-provider-neutral embedding protocol. |
| `Chunker` | Synchronous document splitting protocol. |
| `FixedSizeChunker` | Default overlapping character chunker. |
| `RAGBackend` | Lifecycle and datastore protocol for provider implementations. |
| `PostgresBackend` | Built-in durable backend. |
| `MemoryBackend` | Built-in bounded development backend. |
| `Scalar` / `FilterValue` | Portable metadata and filter type aliases. |

## Validation and safety bounds

The shared contracts reject ambiguous or unbounded input before it reaches an
embedder or datastore. Current limits are:

- 1,000 documents and 50 million source characters per ingestion request;
- 10 million characters per document and 1 million per chunk;
- 10,000 generated chunks per ingestion request;
- embedding batches from 1 to 1,000 and vectors up to 65,536 dimensions;
- search limits from 1 to 100 and query text up to 16,384 characters;
- 64 metadata or filter fields and 100 values per membership filter; and
- safe, bounded document, chunk, namespace, filter, and table identifiers.

Inputs also reject empty text, duplicate identities, nested metadata, boolean
vector values, and NaN or infinite numeric values.

Source and issue tracking live in the
[Usefused/harnest repository](https://github.com/Usefused/harnest).
