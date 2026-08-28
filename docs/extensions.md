# Runtime extensions

Root `extensions/**/*.py` files add application lifecycle behavior without
adding tools, agents, MCP clients, or skills. Files and nested directories may
have any valid public Python name. Harnest ignores ordinary helper functions and
discovers only functions decorated through `harnest.lifecycle`.

Every agent declares one session-store and one checkpointer factory. `harnest
init` keeps both in `extensions/storage.py` and returns the shared development
store from `lib/storage.py`:

```python
from harnest.lifecycle import lifecycle
from harnest.lib.storage import store


@lifecycle.session_store
def session_store():
    return store


@lifecycle.checkpointer
def checkpointer():
    return store
```

The synchronous zero-argument factory runs once when the compiled
application is created. Harnest starts a shared resource once, uses it across
JSON, SSE, WebSocket, and framework adapters, and closes it with the
application. Replace `MemoryStore` with a durable built-in or custom store for
production. The listeners may be split across files, but duplicate,
asynchronous, incorrectly typed, or missing factories fail compilation.
Session storage cannot also be injected by the host because that would create
competing authorities. See [checkpoints.md](checkpoints.md).

## Output policy

Harnest suppresses intermediate subagent narration attached to tool calls by
default. Tool calls, tool results, and the canonical reply remain visible. A
use-case team that intentionally presents that narration can opt in with one
optional root factory:

```python
# extensions/output.py
from harnest.lifecycle import lifecycle
from harnest.output import OutputPolicy


@lifecycle.output_policy
def output_policy():
    return OutputPolicy(subagent_messages="include")
```

The policy applies equally to managed and advanced roots and their subagents,
and the neutral JSON, SSE, WebSocket, and playground surfaces share the same
selected output. The factory must be synchronous, accept no arguments, and
return `OutputPolicy`; duplicate factories fail compilation. It does not alter
the authored `agent`, `tools`, `client`, or `smoke` test fixtures.

```python
# extensions/history.py
from harnest.lifecycle import lifecycle


@lifecycle.before_invoke(order=10)
async def apply_policy(context, request):
    return request


@lifecycle.after_invoke(order=20)
async def persist_result(context, result):
    await save(result)
    # None observes without replacing the result.
```

A file may define multiple listeners, and multiple files may listen to the same
phase. Execution order is explicit `order`, then relative file path, source
line, and function name. Put shared implementation in root `lib/` and import it
through `harnest.lib.*`. Nested agents cannot own extensions.

## Context resources

Use `@context("name")` on a zero-argument provider to publish its returned value
throughout one invocation:

```python
from harnest.context import context


@context("request_cache")
def request_cache():
    return {}
```

The provider runs once at the start of each invocation. Any node, tool, lifecycle
listener, or subagent executing inside that invocation can retrieve it:

```python
from harnest.context import context


async def recall_node(state):
    memory = context.resource("memory")
    facts = await memory.search(state["question"])
    return {**state, "memories": facts}
```

Add `@lifecycle.resource` when Harnest should instead own application-scoped
startup and reverse-order shutdown. Lifecycle ownership alone does not publish
the entered value; `@context` makes that exposure explicit:

```python
# extensions/memory.py
import os

from google.cloud import bigquery
from harnest.context import context
from harnest.lifecycle import lifecycle
from harnest.lib.memory.bigquery import BigQueryMemory


@lifecycle.resource
@context("memory")
async def memory():
    client = bigquery.Client(project=os.environ["GCP_PROJECT"])
    memory = BigQueryMemory(client, table=os.environ["MEMORY_TABLE"])
    try:
        yield memory
    finally:
        client.close()
```

Keep `BigQueryMemory` and other reusable implementations under nested root
library paths such as `lib/memory/bigquery.py`; import them as
`harnest.lib.memory.bigquery`. Namespace folders need no `__init__.py`.
Dependencies such as `google-cloud-bigquery` belong in `pyproject.toml`.

Context providers are provider factories, not tools, nodes, agents, or
invocation/model/authentication listeners. Combining those execution roles is
rejected. Names must be unique. Unknown names and calls to
`context.resource(name)` outside an active Harnest invocation fail clearly at
runtime. Storage and checkpointers remain private unless the author deliberately
publishes them with `@context`. That returns the raw implementation, so agent
code must use `context.user_id` and `context.session_id`, respect leases, and
avoid mutating framework checkpoint state directly.
The compiler imports providers without connecting to external services.

Harnest binds the context around `/responses`, `/live`, direct
`run_agent_message` calls, and their managed nodes, tools, lifecycle listeners,
and subagents. A framework-native endpoint or native ADK/LangGraph target called
outside that boundary has no Harnest context. Advanced code that needs context
must therefore run through the compiled Harnest application.

## Portable phases

- `authenticate(connection, principal)` runs once on each protected HTTP/SSE
  request or WebSocket handshake. It returns `AuthPrincipal` or `None`. The
  read-only `ConnectionContext` contains transport, method, path, headers,
  cookies, and query values without exposing FastAPI. The pipeline starts at
  `None`, fails closed if no listener resolves a principal, and never permits an
  established `user_id` to change. With no auth listeners, local anonymous mode
  remains enabled.
- `before_invoke(context, request)` pipelines `InvocationRequest` values.
- `on_event(context, event)` pipelines runtime events; return `DROP_EVENT` to
  suppress one.
- `after_invoke(context, result)` pipelines `InvocationResult`. A streamed
  result is already public, so its after hook is observational.
- `on_error(context, error)` observes failures. Observer failures never replace
  the primary error.
- `before_model(context, request)` receives neutral `ModelCallRequest`; return a
  replacement, `ModelCallResponse` to short-circuit, or `None`.
- `after_model(context, response)` pipelines neutral `ModelCallResponse`.
- `on_model_error(context, error)` observes model failures without masking them.

Managed ADK and LangGraph agents lower portable model listeners through native
model callbacks/middleware while keeping provider request types private. The v1
portable contract transforms visible text or short-circuits a call; it cannot
add/remove messages, change roles, or switch a LangGraph model. Native opaque
fields, tool calls, metadata, usage, IDs, and structured output are preserved.
Use a native decorator for structural control. Arbitrary advanced native model
calls do not receive portable hooks automatically; configure Harnest's LiteLLM
lifecycle or a native plugin/middleware explicitly.

## Native integrations

Use an explicit synchronous zero-argument factory when portable hooks cannot
express a framework feature:

```python
@lifecycle.adk_plugin(order=30)
def audit_plugin():
    from google.adk.plugins.base_plugin import BasePlugin
    return BasePlugin(name="audit")


@lifecycle.langgraph_middleware(order=30)
def guardrail_middleware():
    from langchain.agents.middleware import AgentMiddleware
    return AgentMiddleware()
```

ADK plugins retain all native `BasePlugin` callbacks and are attached in order
to `App.plugins`. LangGraph middleware is passed to managed agent construction.
Advanced LangGraph graphs are already compiled, so middleware must instead be
wired before `Agent.advanced(...)`; Harnest rejects a discovered late factory.
Wrong native return types, duplicate ADK plugin names, symlinks, and public
non-Python extension resources fail compilation. A native factory targeting the
agent's other framework also fails instead of being silently ignored.
