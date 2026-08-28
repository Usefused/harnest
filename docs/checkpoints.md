# Checkpoints and storage ownership

Every compiled agent declares exactly one checkpoint authority with a
synchronous, zero-argument `@lifecycle.checkpointer` factory. Compilation
rejects missing, duplicate, raw, framework-mismatched, and hidden providers.
Changing the provider requires recompiling the immutable agent artifact; the
running server never rewires storage.

`SessionStore` remains the authority for completed multi-turn conversation and
business state. `CheckpointStore` holds private in-progress execution state:
run status, framework snapshots, pending parallel writes, and resumable wait
metadata. A session permits one running or waiting invocation. Status changes
use compare-and-swap so two replicas cannot resume the same run successfully.

## Managed agents

Use one built-in store for both lifecycle contracts:

```python
# lib/storage.py
import os
from harnest.store import PostgresStore

store = PostgresStore(os.environ["DATABASE_URL"])
```

```python
# extensions/storage.py
from harnest.lib.storage import store
from harnest.lifecycle import lifecycle

@lifecycle.session_store
def session_store():
    return store

@lifecycle.checkpointer
def checkpointer():
    return store
```

Both values remain runtime-private by default. Publish direct access only when
agent code genuinely needs the store API:

```python
from harnest.context import context


@lifecycle.session_store
@context("storage")
def session_store():
    return store
```

Managed nodes, tools, and subagents can then call
`context.resource("storage")` inside an active invocation. The same pattern may
publish the checkpointer under a distinct name. This is privileged raw access:
agent code must scope every operation with `context.user_id` and
`context.session_id` and must not mutate checkpoint state behind the framework.
Harnest still owns startup, shutdown, and framework adapter selection.

Harnest starts and closes the shared object once. Managed LangGraph receives a
native `BaseCheckpointSaver` adapter with one thread per invocation and batched
pending-write loading. Managed ADK enables native resumability and records
non-partial invocation events. Final successful state is committed through the
session boundary; checkpoint payloads remain private.

Built-in choices:

- `MemoryStore`: local development and tests; process loss deletes everything.
- `PostgresStore`: recommended production backend. The compiled environment
  includes Harnest's release-bounded `asyncpg` driver. It provides transactional
  CAS, advisory session leases, and versioned schema bootstrap.
- `RedisStore`: distributed sessions, leases, and expiring checkpoints. The
  compiled environment includes Harnest's release-bounded `redis` driver.
  Production durability depends on Redis AOF,
  replication, and failover configuration. One store prefix occupies one Redis
  Cluster hash slot so multi-key Lua CAS remains atomic; a single agent store
  is therefore not sharded across slots.

The standalone artifact is host-neutral. Schema identity is embedded in
`harnest-manifest.json`; it assumes no Harnest deployment orchestrator. A
custom store owns its own schema and migration policy.

## Advanced ownership

Keep the provider in `lib/` and return it from the lifecycle. Advanced
LangGraph with Harnest storage must compile with the exact adapter:

```python
from harnest.lib.storage import store

graph = builder.compile(checkpointer=store.as_langgraph_checkpointer())
root_agent = Agent.advanced(graph)
```

To hand checkpoint ownership to LangGraph, wrap its native saver and use that
exact wrapper for both native graph compilation and the checkpoint lifecycle:

```python
from harnest.checkpoint import LangGraphStore

checkpoints = LangGraphStore(native_saver)
graph = builder.compile(checkpointer=checkpoints)
```

Return `checkpoints` from `@lifecycle.checkpointer`, and keep a separate
`SessionStore` factory for committed conversation state. For
ADK-native ownership, create `ADKStore(session_service)` once in
`lib/storage.py` and return that same object from both storage factories.
Harnest enables ADK resumability and uses the declared service rather than
creating a second session authority.

Framework checkpoints are opaque and cannot be translated between ADK and
LangGraph. A framework switch preserves committed session state, but active
runs must finish or be cancelled. This first contract intentionally excludes
time travel, checkpoint forks, historical state editing, and exactly-once
external side effects. Give side-effecting tools an idempotency key derived
from the run and tool-call identities.

Harnest records privacy-safe OTEL mutation events, never checkpoint payloads,
prompts, arguments, credentials, or results. Current neutral HTTP approvals and
client-tool waits retain the exact in-process task; their wait metadata can be
stored, but restart-safe continuation requires a framework-native interrupt
boundary and is not claimed for arbitrary Python call stacks.
