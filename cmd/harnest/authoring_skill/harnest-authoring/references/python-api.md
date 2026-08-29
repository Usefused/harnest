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
`input_schema`, `output_schema`, `output_key`, `generate_content_config`, and
`history`. `input_schema` and `output_schema` accept Pydantic `BaseModel`
classes. The input model validates `input` consistently for JSON, SSE, and
`/live`; every completed transport exposes validated structured output as
`result`. `history="session"`
(default) includes earlier user/assistant turns from the same Harnest session;
use `history="turn"` for deliberate per-invocation isolation. The behavior is
the same for ADK and LangGraph, including `Agent` nodes in portable graphs. A
graph agent consumes its predecessor's direct output as the current user input;
session mode keeps earlier conversation in addition to that value.

`Graph(..., output_schema=ResultModel)` applies the same validated public result
boundary to its terminal output. To include framework-generated turn details,
declare one explicit runtime-owned field on either output model:

```python
from typing import Any
from pydantic import BaseModel
from harnest import FrameworkMetadata

class TurnMetadata(BaseModel):
    adk: dict[str, Any] | None = None
    langgraph: dict[str, Any] | None = None

class ResultModel(BaseModel):
    answer: str
    metadata: FrameworkMetadata[TurnMetadata]
```

Harnest excludes that field from provider generation, injects the active native
namespace after the turn, and validates the complete model. Without the marker,
result metadata is not injected. Use at most one marker field. Advanced output
is adapter-owned.
Prefer filesystem composition over
manually populating discovered resources. These explicit object fields are not
name-based access selectors for resource folders: filesystem location defines
which discovered resources an agent owns. Harnest does not define a separate
`SubAgent` class.

Define these Pydantic types in root `models/**/*.py`, not beside each use. A
model in `models/support.py` imports as `harnest.models.support`; no
`__init__.py` is required. The same namespace is available from `agent.py`,
tools, client tools, extensions, subagents, tests, and compiled runtime code.

### Multimodal content

Use portable types from `harnest.content` inside those Pydantic models:
`Text`, `Image`, `Audio`, `Video`, `File`, `AssetRef`, and typed `Data[T]`.
Define reusable policies with `Annotated`, for example
`Annotated[Image, ImageConstraints(media_types=frozenset({"image/png"}),
max_bytes=5 * 1024 * 1024, max_width=4096, max_height=4096)]`. Audio, video,
file, and custom data have corresponding constraint classes. The constraints
appear as `x-harnest-media` in JSON Schema.

Without storage metadata, `Image`, `Audio`, `Video`, and `File` accept canonical
base64 in `data` plus a concrete `mediaType`. Harnest inspects and leases those
bytes before framework persistence, injects them only into the immediate model
call, and then clears them. This works for top-level input, ordinary typed tool
output, and client-tool results returned mid-turn by root agents or subagents.
For media consumed as model input, outgoing JSON, SSE, WebSocket events, and
session messages never expose intermediate base64 or private lease IDs;
transcripts use a content-free `"attached"` placeholder. Native framework
checkpoints, history, traces, logs, and audits contain neither bytes nor private
lease IDs. Inline media deliberately declared in the final output model is
returned once on the authenticated response transport, but is not durable or
replayable through session messages. Use `Stored(...)` when it must remain
retrievable.

For deliberate durability, annotate the field with
`Stored(store="media", path="screenshots", expires_in=60,
retention=timedelta(days=7))` and configure the named storage with
`@lifecycle.asset_store(name="media")`. An inline value is saved before it
becomes framework-visible; the durable value carries scoped `assetId` and
`store` fields. A URL-capable storage generates a fresh signed URL only at the
model-call boundary. Agent code can use `context.assets.stat/get/open/delete`
and `context.assets.url` within the active user-and-session scope. Existing
clients may still upload to `POST /sessions/{sessionId}/assets` and send a
reference. See the [multimodal data guide](https://docs.usefused.com/harnest/build/models-and-libraries/typed-multimodal-contracts)
and [media storage guide](https://docs.usefused.com/harnest/build/models-and-libraries/store-and-retrieve-media)
for the complete two-policy lifecycle.

`Graph` nodes may be `Agent` definitions, typed callables, nested `Graph`
objects, `Join()` nodes, accepted backend-native nodes, or strings naming
filesystem-discovered tools and subagents. String references are how a graph
uses a sibling resource without importing it. `Edge` sources use `START` for
entry. A callable can return a plain value or `Event(output=..., route=...,
message=..., state_delta=...)`; routed edges set `route=`. A callable may
declare `context: GraphContext`, read `context.state`, and persist explicit
session changes with `state_delta`. This works identically in managed ADK and
LangGraph without authoring a native plugin or checkpointer; Harnest supplies
the lifecycle-owned framework adapter. Every node must be reachable.

An inline `Agent` node defined in the root `agent.py` is composed in the root
resource scope. For an agent with private tools or skills, define the same
managed `Agent` under `subagents/<name>/agent.py`; its sibling resources remain
isolated from the parent. A flat managed `subagents/<name>.py` has no private
folder and should be promoted when private resources are needed. An advanced
subagent remains flat and composes those capabilities through its native target.

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

Use `@tool(output_schema=ResultModel)` to accept a mapping or model instance and
validate it as that Pydantic type. A direct `-> ResultModel` return annotation
enables the same behavior. `@client_tool` accepts the same option and validates
the caller-submitted output before resuming execution.

An ordinary `@tool` returning a model with `Stored(...)` must use `async def`
because asset persistence is awaited. Harnest rejects a synchronous declaration
rather than changing its direct-call semantics. `@client_tool` is still a
client-executed stub; Harnest awaits storage when its result is submitted.

Tool arguments and results must be structured values. Harnest preserves JSON
values, mappings, lists, tuples, dataclasses, enums, UUIDs, paths, temporal
values, decimals, and typed models exposing `model_dump()`. Unsupported,
recursive, and non-finite values fail explicitly; convert custom objects to a
supported model or mapping at the tool boundary.

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

## Authentication and credentials

Use the separately installed `$harnest-authentication` skill for authentication,
verified principals, incoming credentials, downstream resolution, and their
tests. Do not put tokens in context resources, metadata, sessions, or tasks.

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

For remote servers behind gateways, pass a connection-scoped
`MCPClientLifecycle` to `streamable_http(..., lifecycle=...)` or
`sse(..., lifecycle=...)`. Put the subclass in root `lib/` and import it from
the MCP factory. `start(context)` and `close(context)` may be sync or async and
run once per application lifecycle. The synchronous
`create_http_client(options, context)` must return a fresh
`httpx.AsyncClient`; preserve the supplied headers, timeout, and auth unless the
gateway deliberately replaces them. The adapter owns and closes each returned
session client. Use this seam for custom CA/mTLS, proxy, or `httpx.Auth`
configuration. It behaves the same in ADK and LangGraph, redacts hook errors,
and is rejected for `stdio` clients.

For `@require_human_approval(tools=[...])`, use the MCP server's original tool
names before `prefix=` is applied. Harnest validates the selection after
discovery in both frameworks and fails closed when a name is absent. Identical
connection configurations are rejected even when capability identity or
approval policy differs, preventing duplicate sessions to one configured
server. Managed LangGraph installs the policy as a native MCP tool interceptor,
keyed by server and original tool name immediately before network execution;
it does not intercept ordinary local tools.

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

When evaluation determines whether only part of an operation is risky, use an
async protected block instead of decorating the whole tool:

```python
from harnest.approval import request_human_approval
from harnest.tool import tool


@tool
async def execute_typescript(source: str) -> str:
    """Evaluate policy, then execute permitted TypeScript."""

    risk = evaluate_typescript(source)
    if not risk.requires_approval:
        return await execute(source)
    async with request_human_approval(
        action="typescript.execute",
        message="Execute TypeScript with network access?",
        arguments={
            "sourceHash": risk.source_hash,
            "capabilities": sorted(risk.capabilities),
        },
    ):
        return await execute(source)
```

Evaluation completes before the task suspends and is not replayed after
approval. `action` must be a stable, non-sensitive identifier. Bind the exact
evaluated operation with serializable `arguments`; Harnest retains only their
hash. The message is public to the approver. Only the block is approved, and
leaving it records success or failure. Dynamic approval therefore requires an
async callable and a managed Harnest invocation.

`@client_tool` can be combined with `@require_human_approval` in either
decorator order. The approval boundary runs first; after approval, the host
receives the client-tool request and its result resumes the same invocation.

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
Harnest's neutral execution boundary fail closed. Explicitly decorated
capabilities in advanced roots and subagents keep this governance when reached
through `/responses` or `/live`. Opaque native capabilities, direct native
routes, and detached tasks remain framework-owned and need native approval
wiring.

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

### Custom HTTP routes

Use a root `@lifecycle.http_routes` factory when the application needs a
business-specific FastAPI contract. The synchronous factory accepts exactly one
injected `AgentInvoker` and returns an `APIRouter`. Inside a route, call
`agent.invoke(connection=request, input=..., session_id=..., metadata=...)`.
Never accept `user_id`; Harnest derives identity from the authenticated
connection and uses the same sessions, approvals, client tools, lifecycle,
credentials, limits, and telemetry as `/responses`. Compilation rejects
duplicate and Harnest-owned paths. Subagents cannot own routes. See
`https://docs.usefused.com/harnest/runtime/custom-http-endpoints` for the public
contract and example.

### Public output policy

Keep the default when customers should see tool traces and the final answer but
not provisional subagent narration. To expose narration attached to subagent
tool calls, declare one root factory:

```python
from harnest.lifecycle import lifecycle
from harnest.output import OutputPolicy


@lifecycle.output_policy
def output_policy():
    return OutputPolicy(subagent_messages="include")
```

The factory is synchronous, zero-argument, root-only, and unique. Accepted
values are `"suppress"` (default) and `"include"`. It affects Harnest neutral
JSON, SSE, WebSocket, and playground events equally for ADK and LangGraph;
direct native endpoints are not projected. It never enables hidden reasoning,
hides tool calls/results, or removes a terminal answer. Prefer `"suppress"`
unless the product intentionally displays provisional agent progress.

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

For multiple direct destinations, define repeatable zero-argument
`@lifecycle.telemetry_exporter` factories under root `extensions/`. Each factory
is called only at runtime and returns a uniquely named `TelemetryExporter` with
an OpenTelemetry `traces` exporter, `logs` exporter, or both. Harnest owns batch
processors and flushing. Keep exporter construction and environment reads in
the factory; module import must not connect or perform network I/O. See
`docs/extensions.md#telemetry-exporters` for the complete example. Root-owned
exporters also receive Harnest telemetry produced during subagent execution;
nested agents cannot register competing destinations.

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
mutations. The generated `MemoryStore` declaration is development-only.

Every agent also declares exactly one synchronous `@lifecycle.checkpointer`
factory. Keep a shared built-in store in root `lib/` and return the same object
from both storage factories:

```python
from harnest.lib.storage import store
from harnest.lifecycle import lifecycle

@lifecycle.session_store
def session_store():
    return store

@lifecycle.checkpointer
def checkpointer():
    return store
```

`MemoryStore`, `PostgresStore`, and `RedisStore` implement both contracts and
are imported from `harnest.store`. The compiled environment includes the
release-bounded `asyncpg` and `redis` drivers. Managed Harnest creates native
framework adapters. Advanced LangGraph
compiles with `store.as_langgraph_checkpointer()`, or uses the exact lifecycle-owned
`LangGraphStore(native_saver)`. Advanced ADK native ownership uses
one `ADKStore(session_service)` returned from both storage factories. Hidden,
raw, duplicate, or mismatched authorities fail compilation. Read
`docs/checkpoints.md` for schema and recovery limits.

For other database, vector-store, embedding, or HTTP clients, declare a
zero-argument provider and decorate it with `@context("name")`. On its own the
provider runs once per invocation. Add `@lifecycle.resource` when Harnest must
start it once for the application and close it after framework shutdown.
Lifecycle ownership stays private unless `@context` explicitly publishes the
returned or yielded value. Nodes, tools, lifecycle listeners, and subagents use
`context.resource("name")` during execution. Keep provider imports free of
connection or network work. Duplicate names and incompatible decorator roles
fail compilation; access outside an invocation fails clearly. See
`docs/extensions.md` for the complete pattern and BigQuery example.

Harnest binds context for `/responses`, `/live`, `run_agent_message`, and their
managed nodes, tools, listeners, and subagents. Direct native framework
endpoints and native targets called outside the compiled Harnest application do
not receive it. Storage and checkpointer factories may be combined with
`@context` when deliberate direct access is required; their ownership and
tenant guarantees still apply.

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

The same wrapper embeds a native ADK agent as a managed subagent or a compiled
ADK/LangGraph target as a portable graph node. Do not set root input/output
adapters on an embedded wrapper; the containing agent or graph owns its input
shape. Embedded ADK components accept `BaseAgent`/`BaseNode`, not an `App`; a
subagent wrapper name must match the native agent name. Filesystem advanced
subagents use `subagents/<name>.py`, while nested folders remain managed-only.
Harnest-decorated capabilities retain approval, identity, session, and trace
context through the neutral invocation boundary at either depth.

Import ADK or LangGraph directly to construct `target`; Harnest does not wrap or
re-export their APIs. ADK targets are validated ADK applications, agents, or
nodes. LangGraph targets are compiled `Pregel` applications; conventional
`messages` state needs no adapter, while custom state should define neutral
input/output adapters. Portable `harnest.graph.Graph` construction remains a
managed-mode feature.
