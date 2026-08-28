# Compiler-provided Python API

Read this reference when editing authored Python or selecting a resource type.
Every symbol must be imported explicitly.

## Root agents and portable graphs

```python
from harnest.agent import Agent
from harnest.graph import START, Edge, Graph, GraphContext
from harnest.model import LiteLLMModel


root_agent = Graph(
    name="support",
    nodes={
        "respond": Agent(
            name="responder",
            model=LiteLLMModel("ollama_chat/qwen3.5:cloud"),
            history="session",
        ),
    },
    edges=(Edge(START, "respond"),),
)
```

`Agent` is an alias of `AgentDefinition`. Common fields are `name`, `model`,
`instruction`, `description`, `tools`, `subagents`, `mcp`, `sandbox`,
`output_key`, `generate_content_config`, and `history`. `history="session"`
(default) includes earlier user/assistant turns from the same Harnest session;
use `history="turn"` for deliberate per-invocation isolation. The behavior is
the same for ADK and LangGraph, including `Agent` nodes in portable graphs.
Prefer filesystem composition over
manually populating discovered resources. These explicit object fields are not
name-based access selectors for resource folders: filesystem location defines
which discovered resources an agent owns. Harnest does not define a separate
`SubAgent` class.

`Graph` nodes may be `Agent` definitions, typed callables, nested `Graph`
objects, `Join()` nodes, accepted backend-native nodes, or strings naming
filesystem-discovered tools and subagents. String references are how a graph
uses a sibling resource without importing it. `Edge` sources use `START` for
entry. A callable can return a plain value or `Event(output=..., route=...,
message=..., state_delta=...)`; routed edges set `route=`. A callable may
declare `context: GraphContext`, read `context.state`, and persist explicit
session changes with `state_delta`. This works identically in managed ADK and
LangGraph without a native plugin or checkpointer. Every node must be reachable.

An inline `Agent` node defined in the root `agent.py` is composed in the root
resource scope. For an agent with private tools or skills, define the same
`Agent` under `subagents/<name>/agent.py`; its sibling resources remain isolated
from the parent. A flat `subagents/<name>.py` has no private folder and should be
promoted to that directory form when private resources are needed.

## Tools

```python
from harnest.tool import tool


@tool
def lookup_ticket(ticket_id: str) -> str:
    """Return the current ticket summary."""
    return repository.lookup(ticket_id)
```

Place this in `tools/lookup_ticket.py`; the callable and file stem must match.
A tool needs a docstring or `@tool(description="...")`. The decorator keeps the
function directly callable for unit tests.

For work implemented by the connected browser, desktop, or mobile client,
declare a typed stub instead:

```python
from harnest.tool import client_tool


@client_tool
def browser_open(url: str) -> dict[str, str]:
    """Open a URL in the connected browser and return visible page data."""
    ...
```

Harnest never executes the stub body. The managed runtime suspends the exact
invocation and sends its name/arguments through JSON, SSE, or `/live`. HTTP
clients submit `{"output": ...}` to `/client-tools/{requestId}`; WebSocket
clients answer `client_tool.requested` with `client_tool.result`. Unit-test the
client implementation separately from this declaration. Client tools fail
closed outside managed runtime execution.

## Reusable library modules

Use root `lib/` for ordinary Python shared by tools, agents, graph callables,
extensions, or other resources:

```python
# lib/validation.py
def normalize_ticket_id(value: str) -> str:
    return value.strip().upper()

# tools/lookup_ticket.py
from harnest.lib.validation import normalize_ticket_id
from harnest.tool import tool


@tool
def lookup_ticket(ticket_id: str) -> str:
    """Return the current ticket summary."""
    return repository.lookup(normalize_ticket_id(ticket_id))
```

Harnest mounts `lib/<name>.py` as `harnest.lib.<name>` and nested paths such as
`lib/storage/queries.py` as `harnest.lib.storage.queries`. It does not discover
library callables, so helpers cannot become model tools accidentally. `lib/` is
root-only and shared across managed and advanced bundles; do not add
`__init__.py` merely for package discovery or create a nested agent `lib/`.
Imports resolve during compile, tests, evals, and standalone serving.
Third-party dependencies used by library code still belong in
`pyproject.toml`.

## Models

`harnest.model.LiteLLMModel(provider/model, **completion_args)` is the default
provider-neutral connector. `OllamaModel` is an optional convenience that still
routes through LiteLLM. Pass provider options such as `api_base` and `api_key`
through the connector or runtime environment. Managed ADK and LangGraph both
resolve a `ModelConnector` lazily without contacting the model during compile.

Set `thinking=True` to enable model reasoning, `thinking=False` to disable it,
or omit the option for the provider default. Use `reasoning_effort="low"`,
`"medium"`, or `"high"` instead when the provider supports explicit levels; do
not combine it with `thinking`. Hidden thought parts never belong in tools,
customer responses, streams, or eval expectations. Treat
`Agent completed without customer-facing output` as a provider/model completion
failure: the model reasoned but did not emit a final answer or structured result.

For programmable gateway access, subclass `LiteLLMLifecycle` and pass one
instance as `lifecycle=`. `create_transport(context)` runs once per built model
and returns the provider SDK client that LiteLLM accepts through `client=`.
`before_request(request, context)` may mutate the call mapping or return a
replacement. `after_response`, `on_error`, and `close` handle normalization,
diagnostics, and cleanup. Use async hooks for Harnest's async runtime; use only
ordinary hooks when invoking a LangChain model synchronously. A built model
rejects mixed sync/async use, and a lifecycle cannot be combined with a direct
`client=` completion argument. For streams, `after_response` observes successful
exhaustion and `on_error` observes iteration failures. Shutdown waits for active
calls and a failed cleanup can be retried.
`LiteLLMContext` exposes the qualified `model`, selected `framework`, and the
created `transport` (which is `None` during `create_transport`).

Build mTLS at the provider-client boundary rather than in Harnest networking:

```python
import os

import httpx
from openai import AsyncOpenAI

from harnest.model import LiteLLMLifecycle


class Gateway(LiteLLMLifecycle):
    async def create_transport(self, context):
        self.http = httpx.AsyncClient(
            cert=(os.environ["MODEL_CERT"], os.environ["MODEL_KEY"]),
            verify=os.environ["MODEL_CA"],
        )
        return AsyncOpenAI(
            base_url=os.environ["MODEL_URL"],
            api_key=os.environ["MODEL_TOKEN"],
            http_client=self.http,
        )

    async def before_request(self, request, context):
        request.setdefault("extra_headers", {})["X-Team"] = "support"
        return request

    async def close(self, context):
        await self.http.aclose()
```

## MCP clients

```python
import os

from harnest.mcp import MCPClient


def client():
    return MCPClient.streamable_http(
        os.environ["KNOWLEDGE_MCP_URL"],
        headers={"Authorization": f"Bearer {os.environ['KNOWLEDGE_MCP_TOKEN']}"},
        tools=("search",),
        prefix="knowledge",
    )
```

Available constructors are `stdio`, `streamable_http`, and legacy `sse`.
`${ENV_VAR}` placeholders are resolved when connecting, not during discovery.
Each file exports exactly one literally zero-parameter `client()` factory; its
filename is the local client identity. Optional parameters, `*args`, and
`**kwargs` are rejected. The factory remains ordinary Python, so direct
`os.environ`, `os.getenv`, custom credentials, and third-party code are
available. Factory failures identify the exception type without echoing its
potentially sensitive message. Use a prefix to control exposed names; Harnest
separately assigns a stable path-scoped capability identity so same-named
clients in direct, plugin, and subagent scopes cannot collide.

For `@require_human_approval(tools=[...])`, use the MCP server's original tool
names before `prefix=` is applied. Harnest validates the selection after
discovery in both frameworks and fails closed when a name is absent. Identical
connection configurations are rejected even when capability identity or
approval policy differs, preventing duplicate sessions to one configured
server.

## Human approval

Declare protected local tools beside their implementation:

```python
from harnest.approval import require_human_approval
from harnest.tool import tool


@tool
@require_human_approval(message="Approve deleting {customer_id}?")
def delete_customer(customer_id: str):
    """Delete one customer."""
    ...
```

Decorate an MCP `client()` to protect every remote tool or a selected list:

```python
@require_human_approval(
    tools=["merge_pull_request"],
    message="Approve this GitHub write?",
)
def client():
    return MCPClient.streamable_http(os.environ["GITHUB_MCP_URL"])
```

The neutral JSON response returns `status: requires_action`; SSE and `/live`
emit `approval.requested` before `response.completed`. Submit `approve` or
`deny` to `POST /approvals/{approvalId}`. Approval binds to the authenticated
user, session, invocation, action, and argument hash, is consumed once, and
expires fail-closed. Approving continues the exact task suspended before the
call; it does not rerun the invocation or repeat earlier side effects. Each
later protected call creates a separate approval. Unique request IDs,
arguments, messages, credentials, and results never enter approval audit logs.
Pending approvals and suspended tasks are process-local. Calls outside
Harnest's managed execution boundary fail closed. Advanced apps have no
automatic Harnest capability wrapper, including through neutral `/responses`
and `/live`; keep protected capabilities managed or implement framework-native
approval explicitly.

## Plugins versus extensions

A plugin is a filesystem bundle of MCP client modules and progressive skills.
There is no Python plugin object and no plugin agent.

An extension changes application behavior. Arbitrary public Python files below
root `extensions/` may contain helpers, but only decorated functions execute:

```python
from harnest.lifecycle import lifecycle


@lifecycle.after_invoke(order=20)
async def after_invoke(context, result):
    await store.write(context.invocation_id, result.text)
```

Multiple listeners may share a phase. Return `None` to keep a transforming
value, a replacement to transform it, and `DROP_EVENT` from `on_event` to
suppress an event. `authenticate` resolves the existing `AuthPrincipal` from a
read-only connection context. `before_model`, `after_model`, and
`on_model_error` receive only Harnest-neutral types and are guaranteed for
managed model boundaries. Portable v1 changes visible text or short-circuits;
structural message/model control requires zero-argument `@lifecycle.adk_plugin` or
`@lifecycle.langgraph_middleware` factories for native framework callbacks.
Read `docs/extensions.md` for phase and advanced-mode guarantees.

## Logging, tracing, and sandboxing

```python
from harnest.logging import get_logger

logger = get_logger("tickets", component="support")
logger.info("ticket.loaded", ticket_id=ticket_id)
```

Structured logs correlate with active OpenTelemetry trace/span identifiers.
Never log credentials or raw sensitive conversation content.

Use `harnest.tracing` for authored spans:

```python
from harnest.tracing import get_tracer, span, traced


with span("catalog.lookup", product_id=product_id):
    result = lookup(product_id)


@traced("catalog.refresh")
async def refresh_catalog() -> None:
    ...
```

`span` accepts `attributes=`, optional `kind=`, and structured keyword
attributes. `traced` supports synchronous, asynchronous, generator, and async
generator functions. `get_tracer` exposes a dynamic tracer and
`current_trace_ids()` returns the active W3C identifiers when recording.

`harnest.sandbox.Sandbox.container(...)` and `Sandbox.provider(...)` define lazy
ADK code executors. A sandbox is an execution boundary, not merely a policy
flag; provider packages and Docker requirements belong in `pyproject.toml`.

## Production runtime resources

Keep identity and persistence as separate concerns. Pass an
`Authenticator` to `harnest.runtime.create_fastapi_app`; it must validate the
HTTP or WebSocket connection and return `AuthPrincipal(user_id=...)`. The
principal scopes neutral session and execution routes. Do not derive identity
from session payloads or use a session store as an authenticator.

Exactly one synchronous, zero-argument `@lifecycle.session_store` factory must
return a `SessionStore` with tenant-scoped CRUD and an exclusive execution
lease. Harnest owns that instance and adapts it to ADK or LangGraph; host
storage injection is mutually exclusive. Production
stores must persist durably, list with set-based queries, coordinate leases
across replicas, and emit privacy-safe OTEL audit signals after committed
mutations. The example `InMemorySessionStore` declaration is development-only.

## Advanced applications

Use the same public `Agent` type in advanced mode:

```python
from harnest.agent import Agent


root_agent = Agent.advanced(
    name="support",
    target=compiled_graph,
    input_adapter=to_graph_input,
    output_adapter=from_graph_output,
)
```

Import ADK or LangGraph directly to construct `target`; Harnest does not wrap or
re-export their APIs. ADK targets are validated ADK applications, agents, or
nodes. LangGraph targets are compiled `Pregel` applications; conventional
`messages` state needs no adapter, while custom state should define neutral
input/output adapters. Portable `harnest.graph.Graph` construction remains a
managed-mode feature.
