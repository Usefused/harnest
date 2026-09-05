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
`instruction`, `description`, `tools`, `subagents`, `mcp`, `sandbox`, `sandboxes`,
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
function directly callable for unit tests. Describe each parameter's semantic
role in the docstring, especially when two arguments share a type or differ only
as a key/value pair. Expose supported filters, pagination, and ordering as typed
parameters or a separate typed tool; never rely on instructions that encourage
the model to invent undeclared arguments. Managed ADK rejects unknown tool
arguments before its native adapter can discard them.

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

## Lifecycle and scoped state

Root `lifecycle/**/*.py` are discovered independently, so storage connections
need not share one file. Declare roles with `@lifecycle.storage.sessions`,
`.checkpoints`, `.assets("media")`, and `.custom("users")`. Decorators may be
stacked when one connection safely fulfils multiple roles; otherwise keep each
factory beside its responsibility. Sessions and checkpoints are framework
authorities and are never returned by `context.storage`.

Use `context.session.get/set/update/delete` for JSON-safe application data that
must persist with the current session without entering prompts, framework state,
or model-visible history. Use `.namespace("plugin")` to avoid key collisions.
Use `context.storage("users")` or `.resource("users", ExpectedRepository)` for
an explicitly named custom store. Custom stores should expose domain repository
methods rather than leaking SQL connections into agent code.

Portable lifecycle namespaces include `lifecycle.agent`, `lifecycle.model`,
`lifecycle.tool`, `lifecycle.mcp`, and `lifecycle.http`. New tool, MCP, and HTTP
interceptors must return
`context.next()`, `context.next(replacement)`, or `context.finish(result)`;
omitting a transition is an error. Model and legacy flat invocation hooks retain
their existing `None` compatibility. HTTP request replacements use
`HTTPCallRequest`; response-head replacements use `HTTPResponseHead`, while
`finish(...)` requires a complete ASGI response.

`context.assets("media")` selects a named store; references retain their owning
store and an optional domain label. `context.credentials` remains a private
typed resolver. `context.mcp("billing")` exposes no raw transport and is
available only when the runtime can dispatch through the governed tool path.
Implement changing catalogs as `SkillSource` classes under `harnest.lib`, then
register them once with `@lifecycle.skills.source("name")`. Filesystem and
dynamic sources share `context.skills` plus the model's progressive list, load,
and resource tools. Sources must filter by `SkillContext` identity and paginate
before returning descriptors; never fetch remote catalogs during construction.
Inspect `application.lifecycle_coverage.report()` or `/agent` diagnostics before
relying on native internals, especially in advanced mode.

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
not combine it with `thinking`. Provider-exposed reasoning text is emitted as
separate `thinking` activity, never as a tool input, transcript message, final
answer, or eval expectation. Harnest keeps native signatures, state, and
raw metadata private by default. It emits provider-reported model, provider,
finish reason, and exact input/output/total token counts as normalized
`agent_metadata` activity. Treat
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
clients in direct, agent-plugin, Harnest Extension, and subagent scopes cannot
collide.

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

## Harnest Extensions, Agent Plugins, and lifecycle

A folder with `extensions/<name>/extension.yaml` kind `Extension` is a same-process
Harnest Extension. Its strict `extension:extension` entrypoint exports one public local
`Extension` subclass and the singleton `extension` from `extension.py`; Harnest exposes
that module as `harnest.extensions.<name>`. `requires.extensions` declares local
Harnest Extension dependencies, while `capabilities` declares the lifecycle,
context, content, storage, HTTP, native, policy, and telemetry surfaces it
contributes. See [layout.md](layout.md) for the complete manifest and export.

Harnest Extensions share the agent's interpreter, event loop, `pyproject.toml`, and
dialect solve. A plugin may add a PEP 621 `pyproject.toml` whose name/version
match `extension.yaml`; Harnest resolves its static dependencies with the root
project before compiler imports. It still receives no private environment or
independent lock. Plugins may extend `ExtensionContext`; `extension.context` is available only
during its managed invocation. Async `start(context)` runs after declared
dependencies and `stop()` runs in reverse order. In managed mode, Harnest
auto-composes declared content and flattens each plugin's `lifecycle/` with
root lifecycle into one globally validated universal lifecycle.

`start(context)` can resolve a named custom store with `context.storage(name)`
only when the manifest declares `context.storage`; the store must be registered
separately by a `storage.custom` extension. During an invocation,
`context.extensions("temporal", TemporalContext)` and the singleton's
`extension.context` resolve the same revocable plugin view without exposing a
plugin registry. Wrap committed durable plugin writes with
`harnest.extensions.extension_mutation(extension_name, operation,
trigger="agent" | "user")` so Harnest
emits correlated, privacy-safe audit signals; never put payloads or secrets in
the operation name.

A plugin that adapts an external durable runtime declares
`context.continuations`. Its startup context receives a provider-bound
application port for `complete(external_id, result)`, `fail(external_id,
error_code)`, deterministic `register_schema(schema_id, validate)`, and bounded
`list_pending(...)` reconciliation. Register every result schema during plugin
startup so a different replica can validate the callback. Its
`ExtensionContext.continuations` receives the matching invocation-bound
`suspend(external_id, capability=..., schema_id=..., validate=...)` port. The
plugin should wrap those ports behind its own typed API; the agent may own an
ordinary tool that calls `harnest.extensions.<name>` without the plugin
contributing a tool.

Suspension commits an opaque Harnest continuation and returns
`status: in_progress`; poll `GET /responses/{responseId}?sessionId=...` using
the same authenticated principal. Provider completion and the native framework
checkpoint may arrive in either order; once both are durable, one replica
atomically claims the wait. ADK injects the persisted `FunctionResponse` and
does not restore the Python tool frame. LangGraph re-enters its checkpointed
tool node, so code before the wait and external submission must be idempotent.
Harnest shutdown never owns or cancels the external job. A `HarnestStore`
checkpoint provider is required; opaque native advanced checkpointers cannot
provide this portable ownership boundary.

`@tool(durable=True)` opts an asynchronous managed tool into Harnest's native
durability boundary. ADK receives a long-running function tool and LangGraph
receives checkpointed interrupt identity; a Harnest Extension adapter can use the
active native correlation for replay-safe suspension. The framework may restart
the logical tool/node, so external submissions must remain idempotent. This
does not serialize local variables or a Python stack. Awaiting unfinished
plugin or task work from a non-durable tool fails before a wait is persisted.

## Queued application tasks

Put queue work in root `tasks/<name>.py` and export exactly one same-named
`@task` callable. Calling it directly stays local; `await task.defer(...)`
commits JSON-safe keyword arguments and returns a handle with `status()` and
`cancel()`. `await handle.result()` returns a persisted result immediately or,
inside `@tool(durable=True)`, suspends through the native framework checkpoint.
Use `schedule_in=` for delayed work and `idempotency_key=` for deduplicated
submission. Harnest derives a replay-stable key when a durable tool omits one.
Tasks are services, not model-visible tools.

Harnest installs Procrastinate only when a public task exists and runs it on an
explicit task PostgreSQL URL or the application's unambiguous PostgreSQL store.
`max_retries` is queue retry policy, independent from agent checkpoint replay.
Task execution reconstructs scoped agent identity and declared resources but
never serializes credentials or a suspended Python frame. Keep payloads small,
JSON-safe, and idempotent because workers may retry after failure.

From a task, create a fresh persisted root-agent session with
`session = await context.agent.create_session(state=..., key=...)`, then call
`await session.invoke(input)` or stream with `session.stream(input)`. `key` is
optional and makes child session and invocation identity stable across a task
retry. A cron task uses Harnest's automation identity; a user-deferred task
inherits the invoking user and public metadata. Durable external waits return a
typed pending response. Human approvals and client tools fail closed because
their continuations are process-local after the task returns.

Schedule an existing task from root `cron/<name>.py`:

```python
from harnest import Cron
from tasks.build_report import build_report


daily_report = Cron(
    "0 9 * * 1-5",
    task=build_report,
    arguments={"account_id": "acct_123"},
)
```

The export must match the filename. Cron accepts a strict numeric five-column
expression with wildcards, lists, ascending ranges, and steps; timezone is UTC.
Static arguments are signature-checked and remain in Harnest's private payload
store. Only long-lived serving owns schedules; a one-shot local invocation may
execute tasks but does not activate cron.

An **Agent Plugin** below `plugins/` uses the Agent Plugins 1.0 `plugin.json`
manifest, optional `mcp.json`, and optional `skills/`. Harnest maps supported
components into its managed runtime without importing a Python plugin object.
Use `extensions/` for Harnest-specific Python and lifecycle behavior. Standard
MCP settings are literal except for `${PLUGIN_ROOT}` and `${PLUGIN_DATA}` in
stdio arguments, environment values, and working directories.

In advanced mode, Harnest Extensions participate only at Harnest-owned neutral
boundaries. A capability declaration cannot intercept opaque native framework
model, tool, MCP, graph, checkpoint, or subagent execution; the author wires
those paths.

An extension changes application behavior. Arbitrary public Python files below
root or Harnest Extension `lifecycle/` may contain helpers, but only decorated
functions execute:

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
the lifecycle context names the managed agent or subagent performing that
specific model call while retaining the root invocation's authenticated user,
session, and invocation identity.
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
contract and example. Keep domain-specific session queries, such as filtering
by a site's origin, in an authenticated custom route backed by an
application-owned index. Do not fetch one neutral `/sessions` page and filter
it locally because matches can exist on later pages.

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

The factory is synchronous, zero-argument, root-only, and unique.
`subagent_messages` accepts `"suppress"` (default) or `"include"`.
`agent_metadata` accepts `"normalized"` (default) or `"raw"`; raw mode keeps the
portable fields and additionally exposes JSON-safe ADK or LangGraph metadata.
It affects Harnest neutral JSON, SSE, WebSocket, A2A streaming, local responses,
and playground events; direct native endpoints are not projected. Raw provider
metadata may contain sensitive or high-cardinality values, so enable it only
for callers authorized to receive the native payload. Harnest-owned
checkpoints persist the normalized completion snapshot for replica-safe polling.
Raw metadata remains ephemeral unless raw mode is combined with
`persist_raw_agent_metadata=True`.

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
`@lifecycle.telemetry_exporter` factories under root `lifecycle/`. Each factory
is called only at runtime and returns a uniquely named `TelemetryExporter` with
an OpenTelemetry `traces` exporter, `logs` exporter, or both. Harnest owns batch
processors and flushing. Keep exporter construction and environment reads in
the factory; module import must not connect or perform network I/O. See
`docs/extensions.md#telemetry-exporters` for the complete example. Root-owned
exporters also receive Harnest telemetry produced during subagent execution;
nested agents cannot register competing destinations.

Use public domain modules for authored imports: `harnest.auth` for principals,
`harnest.http` for route/lifecycle contracts, `harnest.server` for configuration,
`harnest.tool` for client tools and tool lifecycle types, `harnest.mcp` for MCP
contexts, `harnest.model` for model contexts, `harnest.context` for scoped context
types, `harnest.runtime` for runtime contracts, `harnest.assets` for `Stored`, and
`harnest.store` for storage contracts. Query lifecycle guarantees with
`lifecycle.coverage(...)`. Avoid implementation-module paths in new examples.

`harnest.sandbox.Sandbox.provider(...)` defines a lazy, framework-neutral
executor contract. Concrete providers are Harnest Extensions. For Docker, run
`harnest extensions install docker --project <agent-root>` and then
`harnest env sync <agent-root>`. Define `sandbox/<name>.py` with a matching
variable and explicitly assign `Agent(sandboxes=["<name>"])` on every allowed
agent. A Docker definition uses the installed public namespace:

```python
from harnest.extensions.docker import docker
from harnest.sandbox import SandboxNetworkPolicy


calculations = docker.sandbox(
    image="python:3.12-slim",
    network_policy=SandboxNetworkPolicy.none(),
)
```

Names are 1–47 ASCII identifier characters. Named assignments do not expose
model tools. Inside an authored tool, import `context` from `harnest` and call
`context.sandboxes["<name>"].execute(code, input_files=())` or await `aexecute`
with the same arguments. Both return `SandboxResult`; check `status` and choose
which stdout, files, or safe metadata fields to return to the model. The tool's
surrounding Python still executes on the agent server.
Root catalog names are available to all same-project agents, including flat,
nested, graph, and code-defined subagents; permissions are never inherited.
Child catalogs may add local names but cannot duplicate ancestor names.
No assignments means no named sandbox access, even with a populated folder.
Access requires a managed invocation: unassigned names raise
`ContextResourceError`; wrong/revoked invocation handles raise
`ContextUnavailableError`. Backend failures raise sanitized
`SandboxExecutionError`; provider stderr stays in `SandboxResult`.
`Agent(sandbox=...)` is removed. Declare sandbox/<name>.py, grant `sandboxes=["<name>"]`, and call it from an authored tool.

Provider scopes use `from harnest.sandbox import control`: open execution with
`control.execute(timeout_seconds=...)`, inspect its token with `control.current()`,
and release owned resources with `control.cleanup(timeout_seconds=5)` after
cancellation. Cleanup forbids new execution; pass its `remaining()` budget to SDK
I/O and use `check()` before operations. Ordinary nested failures revoke only the
failed scope and descendants; explicit cancellation still propagates. Import
`SandboxCancelledError` from `harnest.sandbox` when handling revoked work.

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

Custom `CheckpointStore` implementations require the complete `RunScope`
(`application_id`, `user_id`, `session_id`, and `run_id`) for every operation
after `begin_run`. Apply all four values in the datastore predicate and return
the same not-found result for missing and foreign runs. Never expose a helper
that loads checkpoints by `run_id` alone.

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
