# Harnest RAG Extension

The official RAG Harnest Extension gives Tools and Tasks one typed ingestion and
retrieval API backed by PostgreSQL. It also defines the `RAGBackend` contract
that future datastore-specific Harnest Extensions can implement. A bounded
in-memory backend supports local tests. This is a Harnest Extension,
not an Agent Plugin.

Install it into an agent project and refresh the environment:

```bash
harnest extensions install rag --project my-agent
harnest env sync my-agent
```

Create the service in a lifecycle module. The namespace resolver runs for every
operation, so user or tenant isolation can follow the active invocation:

```python
import os

from harnest import context, lifecycle
from harnest.extensions.rag import RAGDocument, RAGService, rag
from my_agent.lib.embeddings import embeddings


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

Trusted Tools and Tasks resolve `context.resource("knowledge", RAGService)` and
call `search`, `fetch`, `ingest`, or `delete`. Do not expose mutation methods to
the model unless the authored Tool applies suitable permission and approval
policy.

PostgreSQL uses generated full-text indexes for keyword search and computes
cosine and hybrid scores in the datastore over portable float arrays. This keeps
the extension usable on stock PostgreSQL without requiring a server extension.
`rag.memory(...)` is intended for bounded development and unit tests, not
durable production knowledge.

Custom backends implement `start`, `close`, `replace`, `delete`, `fetch`, and
`search`. `replace` receives complete document identities and chunks so stale
chunks can be removed atomically. Filtering, ordering, and limiting belong in
the backend query. A separate provider extension lists `rag` under
`requires.extensions` and uses the same contract instead of redefining the RAG
API.

Read the complete guide at
[docs.usefused.com/harnest/build/extensions/official/rag](https://docs.usefused.com/harnest/build/extensions/official/rag).
Source and issue tracking live in the
[Usefused/harnest repository](https://github.com/Usefused/harnest).
