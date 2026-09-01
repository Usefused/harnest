# Architecture

## Control flow

```text
orchestrator.py (zero-import source)
      │  harnest plan (injected globals; DeploymentPlan JSON on stdout)
      ▼
Go runtime ── discovers agent folders ──► strict YAML + file validation
      │                                      │
      │                                      └─► stable source digest
      ▼
engine.Deployer
      │
      └─► compile source ─► framework-selected artifact ─► neutral server
```

The plan renderer imports user-authored Python, so the runtime must treat
`orchestrator.py` as trusted project code. It is a Python deployment declaration,
not the long-running orchestrator engine. The JSON boundary is intentional: it
keeps Python implementation details out of the Go engine and is straightforward
to version, log, sign, or render in CI before deployment. A pre-rendered plan can
also be supplied to the Go executable with `-plan`, avoiding Python execution in
the deployment control plane.

## Ownership boundaries

Python source owns agent behavior through the compiler-provided `harnest.*`
authoring namespace:

- provider-neutral graphs or a concise one-agent definition
- local typed tools
- ordinary reusable Python mounted below `harnest.lib`
- MCP client connections and tool filtering
- prompts and model configuration
- the folders selected for a deployment run

`config.yaml` owns the framework boundary:

```yaml
spec:
  entrypoint: agent:root_agent
  framework:
    name: adk          # adk | langgraph
    mode: managed      # managed | advanced; omitted means managed
```

Both fields are part of the current `harnest.dev/v1alpha1` contract. In managed
mode Harnest composes and lowers source. In advanced mode it validates a
framework-native application created with `Agent.advanced(...)`; it does not
discover or wire managed resource folders. The compiled manifest records the
effective framework name and mode, so the Go engine can reject a mismatched
artifact.

The framework boundary is also versioned by the Harnest release. The release's
package metadata declares supported `google-adk` and `langgraph` ranges. Before
importing the configured entrypoint, compilation reads the selected installed
framework distribution and rejects missing, malformed, or out-of-range
versions. This check applies to both managed lowering and advanced targets:
advanced mode exposes framework-native APIs but does not opt out of Harnest's
tested dependency range.

The generated manifest records the Harnest version and the selected installed
framework version. These values describe the compiler/runtime compatibility
context; they do not replace the source digest or the configured framework and
mode checks.

The general `LiteLLMModel("provider/model", **completion_args)` connector
supports any LiteLLM provider and constructs either ADK's LiteLLM adapter or a
LangChain `ChatLiteLLM` adapter lazily. The example
uses `ollama_chat/qwen3.5:cloud`; `LITELLM_API_BASE` selects the endpoint. The
deployment engine injects both values from the manifest's non-secret environment
map. A production deployment can point them at an Ollama sidecar or network
service without changing the agent package. The default `qwen3.5:cloud` request
still goes through the local Ollama API; the daemon owns the user's Ollama Cloud
sign-in and forwards the request.

The connector's optional `thinking` switch is framework-neutral: `True` maps to
LiteLLM's medium reasoning effort, `False` maps to `none`, and omission preserves
the provider default. Explicit `reasoning_effort` remains available for finer
provider control and cannot be combined with `thinking`. Framework drivers may
retain hidden reasoning in their native conversation state, but the neutral
response normalizers expose only customer-facing text and structured output. A
reasoning-only completion fails the public response contract instead of
becoming a successful response with an empty body.

`LiteLLMModel(..., lifecycle=LiteLLMLifecycle())` is the programmable gateway
boundary. Harnest wraps ADK's per-model `LiteLLMClient` or the individual
LangChain adapter client, never LiteLLM module globals. `create_transport` runs
once per built model and its result is supplied as LiteLLM's provider `client`;
`before_request`, `after_response`, and `on_error` wrap each completion, while
`close` runs during runtime shutdown. This supports provider SDK clients backed
by mTLS/custom HTTP transports without coupling Harnest to certificate or
gateway configuration. Sync hooks serve sync calls; async hooks require the
framework's async path, and a model instance rejects mixed execution modes.

## Filesystem-first composition

An agent folder is itself the Python authoring unit; there is no required wrapper
package. Its `agent.py` creates only the root definition:

```python
from harnest.agent import Agent

root_agent = Agent(...)
```

That `Agent` is the supported one-agent managed form. Harnest builds an ADK
`LlmAgent` or a LangGraph/LangChain tool-loop agent depending on
`spec.framework.name`. Its portable `history` field defaults to `"session"`,
making earlier user and assistant turns in the Harnest session available to the
model. `history="turn"` projects only the current invocation. ADK chat modes and
LangGraph message projection remain backend details; neither creates another
session or checkpoint authority. When an agent follows another graph node,
Harnest promotes that predecessor's direct output to the agent's current user
input. Session history remains available in `session` mode; `turn` mode exposes
only the promoted input.

The primary graph-like managed form exports the small neutral graph API:

```python
from harnest.graph import START, Edge, Event, Graph, Join


def classify(value):
    return Event(output=value, route="specialist")


root_agent = Graph(
    name="support_flow",
    nodes={
        "classify": classify,
        "specialist": "order_specialist",
        "complete": Join(),
    },
    edges=(
        Edge(START, "classify"),
        Edge("classify", "specialist", route="specialist"),
        Edge("specialist", "complete"),
    ),
)
```

`Graph` validates its node map and edges before lowering. Node values may be
agent definitions, callables, nested graphs, joins, supported backend-native
nodes, or strings naming filesystem-discovered tools/subagents. `START` marks
entry edges. A callable receives the prior node output and may return a plain
value or `Event(output=..., route=..., message=..., state_delta=...)`; routed edges compare
against the event route. A `Join` is the explicit fan-in marker for incoming
branches. `max_concurrency`, when set, is passed to the backend workflow.

A callable that declares `context` receives a framework-neutral
`GraphContext`. Its read-only `state` is the public Harnest session state;
`Event.state_delta` is the only managed graph write path. ADK commits that
delta through event actions, while LangGraph merges it into an internal
namespaced channel and Harnest exposes only public state through session CRUD.
Stateful orchestration therefore needs no ADK plugin. The lifecycle-owned
checkpoint adapter persists execution progress while `SessionStore` remains
the committed public state authority.

The ADK backend lowers this IR to `google.adk.workflow.Workflow`, `Edge`, and
`JoinNode`. The LangGraph backend lowers it to a `StateGraph` with message,
value, route, namespaced session state, a per-invocation native checkpoint
adapter, conditional edges, and LangGraph joins. Nested graph cycles and framework-invalid native nodes are
rejected during lowering.

Advanced source still exports the public `Agent` type:

```python
from harnest.agent import Agent

root_agent = Agent.advanced(target=compiled_graph, name="support")
```

Advanced ADK mode accepts an `App`, `BaseAgent`, or workflow `BaseNode`.
Advanced LangGraph mode accepts a validated compiled `Pregel`. Conventional
LangGraph `messages` state works without adapters; custom state supplies
`Agent.advanced(..., input_adapter=..., output_adapter=...)`. Advanced source
owns framework composition, native lifecycle, and provider-specific
dependencies. Harnest continues to own configured session persistence and the
neutral invocation governance boundary. The same wrapper may appear inside a
managed ADK agent's `subagents` or as a native ADK/LangGraph graph node; embedded
targets use the containing component's input/output shape rather than root
adapters.
Authors import ADK or LangGraph directly to create the target; Harnest does not
wrap or re-export those framework APIs. Building a portable `harnest.graph.Graph`
remains managed behavior and does not require advanced mode.

Before migrating an existing edited project, run the read-only audit:

```bash
harnest mode advanced AGENT_DIR --check
```

The audit reports the current framework, mode, entrypoint, and managed resources
that require explicit framework wiring. It never modifies `config.yaml` or
`agent.py`, regenerates source, or moves resources. The author or coding agent
performs the migration deliberately after reviewing the report.

The root and resource files explicitly import their authoring types but do not
import or register one another. For example, tools use
`from harnest.tool import tool` and MCP client definitions use
`from harnest.mcp import MCPClient`. The compiler/runtime owns this namespace,
so agents do not declare Harnest in `pyproject.toml`. Normal standard-library
and third-party imports remain valid. Root `models/` supplies Pydantic contracts
below `harnest.models`, while root `lib/` supplies reusable implementation below
`harnest.lib`: `models/support.py` becomes `harnest.models.support`, and
`lib/storage/queries.py` becomes `harnest.lib.storage.queries`. Namespace
directories require no `__init__.py`. Import-free resource source fails
compilation; there is no implicit-global compatibility bridge. The bundle always requires a
non-empty UTF-8 sibling `instructions.md`. Managed `Agent` composition supplies
it when the definition omits `instruction`; an explicit nonblank instruction
wins without merging. For a managed `Graph` or advanced application the file
remains required bundle metadata, but Harnest does not inject it into the
framework object. Managed compilation discovers resources relative to an owning
`agent.py`. Paths in this table are relative to that composition folder unless
marked root-only:

| Path | Required filename-matched export |
| --- | --- |
| `models/**/*.py` (root-only) | Pydantic contracts mounted below `harnest.models`; no resource export contract. |
| `lib/**/*.py` (root-only) | Ordinary reusable Python mounted below `harnest.lib`; no resource export contract. |
| `tools/<name>.py` | An `@tool`-decorated callable named `<name>`. |
| `tasks/<name>.py` (root-only) | An application-owned `@task` callable named `<name>`; not exposed to the model. |
| `cron/<name>.py` (root-only) | A same-named `Cron` targeting a discovered root task; strict five-column UTC schedule. |
| `subagents/<name>.py` | Exactly one managed `Agent` with an explicit instruction, or one native `Agent.advanced(...)`, named `<name>`. |
| `mcp/<name>.py` | A literally zero-parameter `client()` factory returning `MCPClient`; the filename supplies local identity. |
| `plugins/<name>/mcp/<client>.py` (root-only) | A plugin-owned `client()` factory using the same rule. |
| `plugins/<name>/skills/<skill>/SKILL.md` (root-only) | Plugin-owned progressive skills. |
| `extensions/**/*.py` (root-only) | Arbitrary public modules containing explicit `@lifecycle.*` listeners/factories and `@context` providers. |
| `sandbox/sandbox.py` | One optional ADK-only `Sandbox` defining the agent's lazy code-execution backend. |
| `skills/<kebab-name>/SKILL.md` | A progressive skill whose frontmatter `name` matches its directory. |
| `evals/<id>.evalset.json` (root test lane) | A test-only ADK `EvalSet`, executable against ADK or LangGraph, whose `eval_set_id` matches its filename. |
| `tests/unit/test_*.py` | Offline agent/tool tests run by `harnest test`. |
| `tests/smoke/test_*.py` | Explicitly enabled live-runtime tests. |

Every optional resource root may be missing, empty, or contain only ignored
files. In those cases compilation moves on without creating a runtime resource.
Once a public entry exists, its complete convention is enforced; populated but
invalid resources are never silently skipped.

The root models and library folders are not resource roots. Their modules are
global to the compiled bundle in managed and advanced mode, while nested agent
`models/` and `lib/` folders are invalid. Their contents are never
auto-registered as tools, nodes, agents, or lifecycle hooks. Entry points and
discovered resources import them explicitly through `harnest.models.*` and
`harnest.lib.*`, with identical resolution during compilation, unit and smoke
tests, evals, and standalone serving. This boundary allows reuse without turning
the authored root into an installable package or coupling independently
discovered resource modules to each other.

Managed ADK and LangGraph validate model-generated tool arguments against the
declared callable schema before native framework coercion. Unknown names return
value-free repair guidance instead of reaching the function or being silently
discarded. Advanced opaque framework tools retain their native validation.

Each MCP module is ordinary Python and may read `os.environ`, call a credential
provider, or construct headers dynamically inside `client()`. Missing required
configuration fails compilation instead of silently removing a capability.
Optional parameters, `*args`, and `**kwargs` are invalid factory signatures.
Factory failures report only the exception type because exception messages can
contain credential or header values. The compiler assigns a deterministic,
path-scoped capability identity independently of the filename, keeping
same-named direct, plugin, and nested-agent clients distinct. Duplicate
connection detection excludes that compiler identity and approval metadata, so
the same configured server cannot be opened twice under aliases.
Literal `${ENV_VAR}` placeholders remain available when connection-time
resolution is preferable. ADK creates `McpToolset`; LangGraph uses
`langchain-mcp-adapters` and defers asynchronous tool discovery until runtime.
The descriptor supports `stdio`, modern `streamable-http`, and legacy `sse`.
ADK receives the matching connection-parameter type; LangGraph receives the
matching `langchain-mcp-adapters` connection dictionary. HTTP request timeouts
and long-lived SSE idle-read timeouts are separate so quiet legacy servers do
not disconnect at the normal request deadline.

A remote descriptor may own one `MCPClientLifecycle` for gateway, proxy,
custom authentication, CA, or mTLS setup. The controller starts it once before
the first adapter session and closes it once after the framework has closed its
MCP resources. The synchronous client factory runs per adapter-owned session
and must return a fresh `httpx.AsyncClient`; the adapter, rather than the
lifecycle, owns and closes that client. This ownership and ordering is identical
for ADK and LangGraph, including startup unwinding and shutdown in reverse
order. A lifecycle is connection-scoped and cannot be shared across different
connections or frameworks. `stdio` rejects HTTP lifecycle configuration.
Hook diagnostics expose only the hook and exception type, while lifecycle
context and HTTP option representations omit URLs, headers, auth, and timeout
objects. Reusable lifecycle implementations belong in root `lib/` and are
attached explicitly by an `mcp/<name>.py` or plugin MCP factory; they are not
discovered runtime extensions.

`@require_human_approval` adds policy metadata to a local `@tool` or an MCP
`client()` factory. Managed ADK wraps function execution and discovered MCP
tools. Managed LangGraph passes an approval interceptor to
`MultiServerMCPClient`, so the gate receives the adapter's original server and
unprefixed tool names immediately before MCP transport execution; ordinary
local LangChain tools never cross that interceptor. The
neutral runtime stops immediately before the call, binds the request to the
principal, session, invocation, action, and canonical argument hash, then
returns `requires_action`. JSON, SSE, and WebSocket use the same approval ID;
`POST /approvals/{approvalId}` atomically denies or grants one matching resume.
An approval continues the exact in-memory task suspended at that protected
call; it never starts the invocation again or replays earlier side effects. A
later protected call creates a new independently bound approval. Timeout,
changed arguments, a second decision, or a second execution fail closed.
Privacy-safe OTEL audit records contain low-cardinality operation, trigger,
outcome, and capability fields, never unique request IDs, arguments, prompts,
rendered messages, results, headers, or credentials.

An asynchronous tool or native callable may instead enter
`request_human_approval(...)` after it evaluates an operation. The async context
manager suspends the same live task at that point, binds the grant to a stable
dynamic action plus canonical evaluated arguments, and records success or
failure for only the protected block. Code before the block is neither approved
nor replayed. This uses the same JSON, SSE, WebSocket, identity, session, expiry,
and one-time decision machinery as declarative approval in both frameworks.

Selective MCP approval names are checked against the server's discovered,
unprefixed remote tool names in both frameworks. A missing name fails closed
before invocation. Each configured LangGraph server is routed to its own policy,
so same-named tools on other MCP servers cannot inherit it. Advanced targets
retain governance for explicitly Harnest-decorated capabilities reached through
the neutral boundary. Opaque native capabilities and direct framework routes
are not inspected and require native approval wiring.
The default approval store and suspended tasks are process local and intended
for standalone development; restarting invalidates pending requests.

Outside root `lib/` and `models/`, the compiler ignores `__init__.py`, dotfiles, cache
directories, and files whose names start with `_`. It imports public resource
files in deterministic filename order. Missing or wrongly typed exports are
convention errors; module import
failures are reported with the resource path; duplicate tool names, subagent
names, or identical MCP configurations fail rather than silently shadowing.
Resources explicitly present on the root definition are kept first, followed by
direct discovered resources and plugins in sorted plugin-name order. Plugins
contribute only MCP clients and progressive skills: the clients expose tools,
while the skills teach the host agent when and how to use those tools. A plugin
must contain at least one of each and never contributes an agent or execution
path. See [Plugins](plugins.md).

Runtime extensions are decorated functions in arbitrary public
`extensions/**/*.py` modules. Multiple listeners share a phase in explicit
order with source path as a deterministic tie-breaker. Portable behavior covers
authentication, invocation, normalized events, errors, and managed model calls.
Explicit ADK-plugin and LangGraph-middleware factories preserve native callbacks
without exposing native request types through portable hooks. Zero-argument
`@context` providers publish invocation-scoped values. Adding
`@lifecycle.resource` transfers application startup and shutdown ownership but
does not publish the entered value by itself. Providers are discovered but not
called by the compiler. See [Runtime extensions](extensions.md).

Zero or more `@lifecycle.skills.source(<name>)` factories register dynamic
`SkillSource` adapters. The synchronous factory is validated during compilation,
but its catalog, documents, and resources are queried only inside a managed
invocation. A source receives revocable agent, verified user and non-secret claim,
session, credential, storage, and resource access through `SkillContext`; it must
apply authorization, search, ordering, and pagination before returning a bounded
`SkillPage`. Harnest applies deterministic fuzzy lexical ranking to filesystem
skills and reranks bounded model-visible descriptors across sources. A dynamic
source remains free to use datastore-native full-text or vector search. When an
initial model-tool query returns no descriptors, Harnest performs one bounded
unfiltered fallback and ranks metadata without loading skill bodies.

Exactly zero or one root `@lifecycle.output_policy` factory selects public
intermediate model messages. Its default `OutputPolicy()` suppresses subagent
narration attached to tool calls without removing tool events or a terminal
answer; `subagent_messages="include"` exposes that provisional narration. Both
backend drivers apply the policy before producing neutral non-streaming and
streaming events, so `/responses`, `/live`, and the playground agree. Native
framework endpoints remain outside this projection boundary.

Zero or more root `@lifecycle.telemetry_exporter` factories declare direct
telemetry destinations. The compiler retains but never calls these factories;
runtime bootstrap resolves each uniquely named `TelemetryExporter` and adds a
batch processor for its optional trace and log exporters. The root owns this
process-wide configuration, including telemetry emitted during subagent work.

Exactly zero or one root `@lifecycle.credential_provider` factory establishes
the application's private downstream-authorization authority. The factory
returns a framework-neutral `CredentialProvider`; construction remains offline,
while `start`, `resolve`, and `close` run only in the compiled application
lifecycle. Resolution receives immutable audience, scope, framework, agent,
invocation, and session identifiers plus the complete verified `AuthPrincipal`,
but never the original connection or request metadata. Selected secret-bearing
authentication values remain opaque `Credential` objects on that principal.
Returned material is redacted, invocation-scoped, and absent from the enumerable
`AgentContext` registry. ContextVar revocation prevents child tasks from
retaining access after their invocation ends. The public
`context.credentials` property is a resolver capability, not stored context
data. See [authentication.md](authentication.md).

The provider is a consumption boundary rather than a persistence API. Workload
identity, Vault, or an external OAuth broker may durably own authorization
grants, but Harnest never saves resolved tokens in sessions, checkpoints,
queued-task payloads, telemetry, or application state. Delayed work resolves a
new short-lived credential from stored non-secret intent or a grant reference.
Managed backends bind the same provider around root and subagent execution;
advanced ADK receives an internal plugin for its native run lifecycle, while
provider start/close remains owned by the outer compiled server. Provider and
lifecycle failures are replaced with type-only errors outside the original
exception chain. The authentication middleware also carries its verified
principal through a separate private, revocable binding. Advanced ADK credential
resolution uses that principal instead of the caller-authored native `userId`;
the native value is only the unauthenticated local-development fallback.

Advanced components continue to own arbitrary native model calls, graph routing,
state/checkpoint semantics, capability declaration, and native integration
wiring. Harnest still owns its neutral HTTP, authentication, session, approval,
tracing, and server boundaries. Explicitly decorated native capabilities inherit
that invocation context whether the advanced component is the root, an ADK
subagent, or a LangGraph node; Harnest does not inspect opaque native targets to
discover capabilities automatically. Managed
LangGraph also rejects ADK sandbox executors, `output_key`,
`generate_content_config`, and implicit subagent delegation rather than
silently ignoring them.

Skills expose progressive list, load, and resource tools instead of placing
every skill body in the root prompt. `FilesystemSkillSource` validates static
folders and content-addresses their complete trees during compilation. Harnest
then composes that source with lifecycle-provided dynamic sources in one
agent-scoped registry. ADK and LangGraph receive the same three governed tools,
and application code uses the same registry through `context.skills`. Catalog
entries contain source, ID, name, description, and version; loaded versions are
pinned for the active invocation. A skill may contain Agent-Skills-style
`references/`, `assets/`, and `scripts/` content. Public entries directly under
`skills/` must be skill directories; symlinks are rejected.

The optional `evals/test_config.json` is an ADK `EvalConfig` shared by both
framework lanes. Eval sets are
sorted and validated during compilation, including unique case IDs, but remain
test-only and never become instructions or runtime tools. `discover_evals()`
also exposes the validated paths for tooling. `harnest test <agent-folder>
--evals` runs the unit suite first, then evaluates every validated eval set in
deterministic filename order through ADK's official evaluator, automatically
applying the optional config. The evaluator's complete built-in metric registry,
user-simulator configuration, judge-backed criteria, and `customMetrics` code
references remain available without a Harnest metric allowlist. Authors never
address temporary compiled paths.
Eval execution is explicit because it can invoke live models and consume
credentials, time, or paid capacity. Harnest passes `num_runs=1` to ADK's
evaluator so a single command does not silently double model usage. The compiled
artifact is temporary and no ADK eval history is persisted; external CI is
responsible for retaining output. Selecting `--evals` without at least one
validated eval set is a convention error.

Harnest names two tool-trajectory policies. `business` is the default and maps
to ADK `IN_ORDER`: every expected business call and argument must occur in
order, while extra discovery or progressive skill calls are allowed. `strict`
maps to `EXACT` and rejects any additional, missing, reordered, or changed call.
The selected policy replaces only the tool trajectory match type; authored
thresholds and other metrics remain intact. CI can run both commands as
separate lanes when both behavioral intent and exact orchestration matter.

The test runner discovers and executes eval assets only from the root bundle's
`evals/`. Nested eval files can be reached by composition validation, but they
are not a nested-agent test lane and are never selected by `harnest test
--evals`.

For LangGraph, Harnest starts the compiled runtime and adapts each text
conversation turn, final response, tool call, and tool result into ADK's
invocation/event contract. This keeps metric computation identical across
frameworks while preserving LangGraph session state between multi-turn eval
invocations. ADK `liveModelConfig` and multimodal user-content simulation remain
ADK-native; LangGraph authors use text conversation evals and opt-in smoke tests
for bidirectional media behavior.

This portable lane is distinct from LangSmith, the LangGraph ecosystem's native
dataset, experiment, online-evaluation, and evaluator service. Harnest does not
silently translate LangSmith datasets or upload eval runs; a future LangSmith
integration must be an explicit provider with its own credentials and retention
contract.

Agent-owned Python tests follow a separate, zero-import test convention.
`harnest test <agent-folder>` compiles the source and collects only
`tests/unit/test_*.py`; `harnest test <agent-folder> --smoke` runs that unit
suite and then `tests/smoke/test_*.py`. Test modules may use normal standard
library and third-party imports but must not import Harnest or load the compiled
package themselves.

The flags compose. `--evals` selects unit tests followed by all eval sets;
`--smoke --evals` selects unit and smoke tests followed by all eval sets. A
failure prevents later lanes from starting and produces a nonzero exit status.

The runner supplies these fixtures:

| Fixture | Suites | Value |
| --- | --- | --- |
| `agent` | unit and smoke | The selected framework's compiled target. |
| `tools` | unit and smoke | A read-only mapping of direct local tool names to their resources. |
| `client` | smoke only | A raw FastAPI `TestClient` for the compiled standalone app. |
| `smoke` | smoke only | Neutral helpers: `.respond(input, session_id=None, metadata=None) -> dict` and `.stream(...) -> list[dict]`. |

One compiled server lifecycle is shared by all selected smoke tests. This keeps
lifecycle-owned session and checkpoint stores available for the entire suite;
Harnest closes them once after pytest finishes. Tests must not close the
injected client or stores. An omitted session ID creates an isolated session;
an explicit ID intentionally carries state and should be unique to one test
so test order cannot change outcomes.

Unit tests are an offline contract: they should exercise definitions and local
tool functions without calling a model, MCP server, or HTTP endpoint. Harnest
reinforces the boundary by withholding HTTP/smoke fixtures, but it does not
install an operating-system network sandbox. Smoke tests are always opt-in
because they can use live models, credentials, MCP services, time, and money.
Their environment must be supplied explicitly by the developer or CI job.
Compiling the unit lane materializes MCP descriptors but does not start an
`MCPClientLifecycle`. A live gateway handshake or remote tool assertion belongs
in the smoke lane. One compiled server lifecycle spans that smoke suite, while
adapter-owned MCP session clients may be created and closed more than once.

Both `tests/` subtrees are test-only compiler input. They never become prompts,
tools, subagents, skill content, evaluation cases, Agent Card capabilities, or
serving routes. A test failure stops the command with a nonzero exit status but
does not mutate the authored agent.

For a recursively composed subagent, use `subagents/<name>/agent.py`, exporting
an `AgentDefinition` named `<name>`. That `agent.py` owns the supported sibling
`instructions.md`, `tools/`, `skills/`, `mcp/`, and `sandbox/` resources in its
folder. Under ADK it may also own child agents in a sibling `subagents/` folder.
LangGraph nested `Agent` definitions cannot consume discovered child subagents
today. Parent resources are not added to the nested definition, and nested
resources are not promoted to the parent.
Plugins and extensions remain root-only; populated nested `plugins/` or
`extensions/` folders are convention errors.

A direct `subagents/<name>.py` and same-named nested folder are mutually
exclusive. A flat subagent has no private resource directory, so it must provide
an explicit instruction and should be promoted to `subagents/<name>/agent.py`
when it needs private tools, skills, or other supported resources. Conversely,
an `Agent` value written inline in the root `agent.py` is root-scoped when used
as a graph node and consumes the root folder's discovered resources.

Filesystem placement is the access model for static skills. There are no
tool/skill name lists on `Agent` that select access to discovered folders, and
no separate `SubAgent` class. An `Agent` becomes a nested subagent through its
folder and its graph/parent relationship. Dynamic sources are application-wide
registrations, but receive the active agent identity and must return only the
skills authorized for that root or subagent.

Compilation writes a separate runtime directory containing the preserved source
tree, generated `agent.py`, `__init__.py`, and `__main__.py` adapters, the
`harnest-agent` launcher, mutable `server.yaml`, and `harnest-manifest.json`. This output is the
selected framework's runtime package. Generated `agent.py` exports the neutral
`CompiledApplication` as `application`, the provider application as `app`, and
the provider target as `root_agent`; its manifest records `framework.name` and
the effective mode. Source does not need `__init__.py` and is loaded through the
generated adapter. Generated `.harnest/`
content is disposable and must not be edited or committed. VCS data, virtual
environments, caches, `.adk/`, `.harnest/`, `.env` files, and bytecode are
excluded. Source symlinks are rejected, keeping artifacts self-contained and
preventing credentials or external files from being pulled in accidentally.

The compiler validates authored `server.yaml` and copies it beside the launcher;
if an older source tree omits it, the compiler materializes the safe loopback
default. The adjacent copy is runtime policy and may be replaced after compile,
so it is the sole regular artifact file excluded from the manifest digest. The
authored copy under `source/` remains hashed, and the Go loader rejects a missing,
symlinked, or non-regular adjacent file plus every other unmanifested file.

The launcher's explicit `serve` command reads this file without additional
flags. `http` controls binding, remote-bind consent, timeout, and concurrency;
`limits.maxRequestBytes` is
enforced across neutral and advanced-native HTTP bodies and WebSocket frames;
and `playground.enabled` controls the bundled UI. Explicit launcher flags are
short-lived operator overrides. Authentication, session storage, TLS, secrets,
and deployment scaling remain separate injection or hosting boundaries.

The launcher also accepts `run` for in-process root-agent invocation. It reads a
bounded prompt from stdin, reuses the final framework-neutral runtime pipeline,
and writes text, JSON, or ordered NDJSON without opening a network listener.
One-shot execution starts task workers when needed but disables periodic
registration so schedule ownership remains with long-lived serving processes.
Tasks receive a separate `context.agent` capability that can create or reopen a
persisted child session through that final pipeline. Retry-stable task identity
prevents duplicate child sessions; cron tasks use a fixed automation principal.
External durable waits return portable pending handles, while process-local
approval and client-tool continuations fail closed after task execution.

Setting scalars may use exact `${NAME}` environment references. Compilation
validates their position and preserves the template bytes; the launcher resolves
them before typed validation. Short `$NAME` syntax and partial interpolation are
rejected so environment content cannot alter YAML structure. Startup errors name
the variable and field without exposing the resolved value.

The artifact can be served without the Go provisioner. Harnest's primary public
surface is deliberately transport- and provider-neutral:

| Route | Contract |
| --- | --- |
| `GET /agent` | Returns `{id,name,description,card,endpoints}` for the compiled agent. |
| `POST /sessions` | Create a session from `{id?,state?}`; returns 201 with the complete neutral session record. |
| `GET /sessions`, `GET /sessions/{id}` | Return self-describing records containing `id`, `userId`, `state`, `createdAt`, `updatedAt`, and namespaced framework `metadata`. Session listing always returns a bounded page with `nextCursor`, defaults to 100 records, and accepts `limit` plus a user-bound opaque keyset `cursor`. |
| `GET /sessions/{id}/messages` | Return a bounded self-describing ordered transcript page as `{sessionId,userId,messages,nextCursor}`; the default and maximum page size is 100, with optional `limit` and a resource-bound opaque `cursor`. Every message has `id`, `role`, `content`, `createdAt`, and complete namespaced framework `metadata`. |
| `PATCH /sessions/{id}` | Apply the exact `{"stateDelta": {...}}` body. |
| `DELETE /sessions/{id}` | Delete a session and return 204. |
| `POST /responses` | Run `input` against an optional `sessionId`; return neutral JSON, or named SSE when `stream` is true. |
| `WS /live` | Direct multi-turn WebSocket transport using neutral connect, request, and response event frames. |

Session `state` remains the portable application-owned view. `metadata.adk`
preserves ADK session fields and events, while `metadata.langgraph` preserves
the complete stored LangGraph state, including native messages and their IDs,
tool fields, response metadata, and usage metadata. These namespaces are
intentionally framework-shaped and may evolve with the release-bounded
framework version; Harnest does not flatten or discard their generated fields.

Managed `Agent` and `Graph` output models may separately opt into current-turn
framework data with one explicit `FrameworkMetadata[T]` field. The compiler omits
that runtime-owned field from the model provider's schema. After completion,
the driver injects the native `adk` or `langgraph` namespace and validates the
full authored output model before any transport exposes it. Models without the
marker receive no injected result field; advanced output remains controlled by
its adapter.

`/responses` also accepts an opaque `metadata` object. Its completed JSON has
`id`, `status`, `sessionId`, `outputText`, ordered `output`, and echoed
`metadata`. An omitted session ID creates a session; a supplied ID must already
exist. Its output-item forms are:

```json
{"type":"message","role":"assistant","content":[{"type":"output_text","text":"..."}]}
{"type":"tool_call","id":"...","name":"...","arguments":{}}
{"type":"tool_result","callId":"...","name":"...","output":{}}
```

Streaming emits
`response.created`, zero or more `response.text.delta`, `response.tool_call`,
and `response.tool_result` events, then `response.completed`; each carries an
increasing `sequence`, `responseId`, and `sessionId`. Text events carry `delta`,
tool calls carry `id`/`name`/`arguments`, tool results carry
`callId`/`name`/`output`, and completion carries the same final output fields as
the JSON response. The SSE `event:` name matches the data object's `type`. A
post-header failure is a terminal named `error` event whose data carries
`type`, `sequence`, response/session IDs, and an `error` string.
Because SSE is server-to-client only, its cancellation signal is closing or
aborting the HTTP request. Harnest then cancels the managed response and awaits
its cleanup; no terminal event can be delivered over the closed stream.

The first `/live` client frame is `{"type":"connect","sessionId":"..."}`
with an optional session ID. The server replies with `session.connected`.
Subsequent client frames are `response.create` with non-empty `input` and
optional `requestId`/`metadata`, or `session.close`. While a response is active,
the client may send `{"type":"response.cancel","responseId":"..."}` using the
ID from `response.created`; stale or malformed IDs fail closed. Harnest awaits
framework and tool cleanup, then emits `response.completed` with
`status: "cancelled"`, empty committed output, and leaves the socket open.
Cancellation is cooperative and does not roll back tool or external side effects
that already completed.
Server response events otherwise use the SSE event names and fields and echo
`requestId`. Invalid frames and execution failures use a typed `error` frame;
policy violations close with WebSocket code 1008. There is no mode flag or
protocol rerouting.

The ADK and LangGraph adapters both emit assistant message/output-text items and
neutral tool calls/results. Provider/model identifiers, reasoning details, and
framework bookkeeping are intentionally omitted. Pre-stream HTTP errors retain
FastAPI's `{"detail":"..."}` shape and normal 4xx/5xx status semantics.

Every mode retains FastAPI's generated `/openapi.json`, `/docs`, and `/redoc`
surface. Managed ADK exposes only the neutral Harnest routes in that schema.
Advanced ADK additionally mounts official ADK session paths, `/run`, `/run_sse`,
and `/run_live`. Those expose ADK-native models and track the installed ADK
version; LangGraph applications do not emulate them. They are not the stable
Harnest integration boundary.

Root extensions may add application routes through repeatable
`@lifecycle.http_routes` factories. Compilation calls each synchronous factory
once with an unbound `AgentInvoker`, validates its FastAPI `APIRouter`, and
rejects duplicate or Harnest-owned paths. Server composition then binds that
same invoker to the final wrapped driver and shared approval/client-tool stores
before mounting the router. Consequently custom routes use the authenticated
principal, session authority, lifecycle hooks, credentials, limits, telemetry,
and continuation semantics shared by ADK and LangGraph; they cannot supply a
separate `user_id` or call the native target directly.

`GET /healthz` and `GET /.well-known/agent-card.json` remain available for
health and card discovery in both frameworks.

`GET /` serves Harnest's dependency-free development playground, with its
bundled assets under `/_harnest/`. The UI depends only on the neutral agent,
session, response, SSE, and live WebSocket contracts, which prevents framework
adapters from developing separate test surfaces. Shell assets are public when
authentication is injected; session and execution APIs remain protected. A
bearer token stays in page memory for HTTP/SSE calls. Browser WebSockets use
same-origin cookie authentication because their API cannot set arbitrary
authorization headers.

When the playground is enabled, its runtime wrapper also retains the latest 50
invocation traces in process memory. The protected `/_harnest/traces` routes
scope results to the authenticated principal and expose normalized runtime
stages, tool events, failures, and authored `harnest.agent.*` log records to the
Trace inspector. The wrapper does not change public agent results and is absent
when the playground is disabled. This short-lived diagnostic buffer complements
rather than replaces OTLP export.

This is a process boundary, not a deployment boundary. The CLI creates a
fingerprinted agent environment containing Harnest, the selected framework,
model adapters, and the dependencies locked from `pyproject.toml`.
Local root-agent invocation is available only when the authored deployment
contract sets `spec.interfaces.cli: true`; that opt-in is bound into the
compiled manifest and its digest. Older or disabled artifacts remain
server-only.
The standalone server does not interpret deployment resources, resolve secrets,
enforce permissions, scale replicas, or choose an identity provider. Session
storage and authentication are separate boundaries. Every compiled application
must contain exactly one root `@lifecycle.session_store` factory. Harnest owns the
returned `SessionStore`, adapts it to the selected framework, and shares it
across neutral and native session routes until shutdown. Ordered root
`@lifecycle.authenticate` listeners or an injected `Authenticator` resolve HTTP
and WebSocket connections to `AuthPrincipal`;
the principal ID scopes all neutral execution and session operations. The
middleware authenticates advanced ADK native routes, but deployment policy must
still authorize their native user fields. Health and discovery remain public.

The generated storage extensions return one shared development `MemoryStore`.
Exactly one `@lifecycle.session_store` and one `@lifecycle.checkpointer`
factory are required. Removing either or declaring duplicates fails
compilation. Production and advanced ownership rules are detailed in
[checkpoints.md](checkpoints.md).
Other database, vector, embedding, and HTTP clients belong in optional
zero-argument context providers. `@context` exposes a value once per invocation;
combining it with `@lifecycle.resource` makes startup and shutdown
application-scoped. Compilation never establishes those external connections.
Production stores must use durable writes, set-based listing, distributed
leases, and emit privacy-safe OTEL audit signals after committed mutations.
The LangGraph driver consumes the store directly. Harnest's ADK session-service
adapter persists native state and event history through that same store, so
advanced native and neutral ADK routes do not create a second authority.
Authentication does not persist sessions, and session storage does not
authenticate callers.

The serving path is deliberately one-way:

```text
CompiledApplication
  ├─ framework target -> ADKRuntimeDriver | LangGraphRuntimeDriver
  └─ RuntimeCapabilities
         -> storage wrapper -> lifecycle/context/credential wrapper
         -> InvocationCoordinator
         -> JSON | SSE | WebSocket | custom HTTP adapters
```

`RuntimeCapabilities` validates and freezes application-owned stores,
credentials, context resources, telemetry exporters, output policy, and custom
routers while `CompiledApplication` retains its established flat attribute
aliases. One runtime-pipeline builder owns wrapper ordering. Only drivers
translate native sessions, inputs, and events.

`InvocationCoordinator` owns caller-scoped session resolution, input/content
validation, request limits, concurrency, deadlines, and non-streaming
continuations. `/responses` and custom `AgentInvoker` endpoints call that same
coordinator. Dedicated continuation, SSE, WebSocket, and session-wire modules
own their respective framing without duplicating invocation policy. The neutral
router assembles these components and owns route registration and shutdown.

Go owns platform behavior:

- deterministic discovery and strict manifest validation
- validation and enforcement of resource, scaling, secret-reference, and permission declarations
- source identity/digests
- bounded concurrent deployment and fail-fast cancellation
- the narrow adapter into the future engine

Consequently, self-service authors add or change only an agent folder and the
Python `orchestrator.py`. The Go runtime and its engine adapter are shared
platform components and do not need one registration branch per agent.

## Deployment contracts

| File or value | Author | Consumer | Purpose |
| --- | --- | --- | --- |
| `orchestrator.py` | Agent/platform user (Python) | `harnest plan` | Select source roots, include/exclude globs, concurrency, and plan metadata. |
| `DeploymentPlan` JSON | `harnest plan` | Go runtime | Stable, versioned language boundary. |
| `config.yaml` | Agent owner | Go runtime and deployment engine | Entrypoint, framework name/mode, Python runtime, resources, scaling, env, secret references, and permissions. |
| `agent-card.yaml` | Agent owner | Deployment engine and A2A clients | Public identity, interfaces, modalities, capabilities, and skills. |
| source `root_agent` | Agent owner in explicitly imported `agent.py` | Harnest compiler | Managed `Agent`/`Graph`, or `Agent.advanced(...)`, selected by config. |
| generated `application` | Harnest compiler adapter | Standalone runtime and engine | `CompiledApplication` containing framework, mode, target, and optional advanced app/bridge. |
| generated `app` / `root_agent` | Harnest compiler adapter | Provider tools and compatibility consumers | Selected provider application and target aliases. |
| `evals/*.evalset.json` | Agent owner | Shared ADK/LangGraph eval CLI and CI | Test conversations and expected behavior; never deployed as capabilities. |

The schemas in `schemas/` are the editor and CI contract. The Go loader also
uses strict YAML decoding and runtime checks. Schema defaults are documentation;
the engine must apply defaults explicitly rather than assuming a validator
mutated the manifests.

The engine should inject `spec.environment` and resolved `spec.secrets`, enforce
the declared network/filesystem permissions, synchronize the dependency project
under the requested Python version, invoke `harnest compile` with the configured
framework and mode, import the generated artifact, and assert that `application`
is a matching `CompiledApplication` before accepting traffic.

## Versioning

Both the deployment plan and per-agent config currently use
`harnest.dev/v1alpha1`. Consumers reject other versions instead of silently
guessing. The JSON representation is documented by
`schemas/deployment-plan.schema.json`. Additive optional fields still require
coordinated Python, Go, and schema changes because strict consumers reject
unknown fields. Breaking field or semantic changes should get a new API version
and an explicit conversion step.

Framework compatibility follows the Harnest release rather than the config API
version. A release publishes bounded ADK and LangGraph dependency ranges and is
tested only inside those ranges. Using a newer unsupported framework therefore
requires upgrading Harnest to a release that supports it; advanced mode and
direct framework imports do not relax this rule. Agent `pyproject.toml` files
cannot declare the selected framework or its compiler-owned adapters; those
packages come only from the release wheel. The exact framework and Harnest versions are
captured in every compiled manifest for deployment diagnostics.

The agent card implements a strict, intentionally small subset of the A2A 1.0
Agent Card: identity, provider, supported interfaces, core capability flags,
modalities, and skills. The YAML file is an authoring convenience; the deployment
layer can publish its JSON representation at
`/.well-known/agent-card.json` and replace environment-specific interface URLs
during deployment. Security schemes, signatures, extensions, tenant routing, and
other optional A2A fields need matching Go types before they can be accepted.

## Discovery and deployment semantics

Each `AgentSource` scans only its immediate child directories. A child becomes a
candidate when it contains `config.yaml`; the corresponding `agent-card.yaml`,
entrypoint module, and required dependency project must then validate. Sources are
kept within `projectRoot`, candidates are sorted, duplicate metadata names fail
the whole discovery run, and disabled agents are validated before being skipped.

`parallelism` bounds concurrent `Deploy` calls. With `fail_fast: true`, the first
deployment failure cancels unscheduled work and signals the shared context to
in-flight deployers; those deployers must honor cancellation. With `false`, all
bundles are attempted and their errors are joined. Orchestrator labels are merged
into each bundle before deployment, with same-named agent labels taking
precedence.

## Engine integration

Start with `engine.CommandDeployer` if the engine already has a CLI. The intended
final adapter is in-process and small:

```go
type engineDeployer struct { client *agentengine.Client }

func (d engineDeployer) Deploy(ctx context.Context, bundle engine.Bundle) error {
    return d.client.Deploy(ctx, agentengine.Deployment{
        Name:      bundle.Config.Metadata.Name,
        SourceDir: bundle.Directory,
        Digest:    bundle.Digest,
        Config:    bundle.Config,
        AgentCard: bundle.Card,
    })
}
```

Keep secret resolution in the engine. The config carries references, never
credentials, and the Go runtime does not expand or log them. The production
adapter must also:

- reuse Harnest environment synchronization or equivalently install the locked
  `dependencyFile` under the requested Python version;
- inject `spec.environment` and resolved `spec.secrets` without logging values;
- enforce resource limits and network/filesystem permissions;
- compile the source folder with the declared framework/mode, import the
  generated `CompiledApplication`, and reject a manifest or application whose
  framework boundary does not match `config.yaml`;
- host the agent and publish the deployed Agent Card endpoint; and
- use the bundle digest as immutable source identity.

## Observability boundary

Source changes follow the project-wide complexity, separation, database-access,
testing, and audit rules in [development.md](development.md). Compiler and CLI
filesystem mutations are currently excluded from OTEL auditing; this avoids
creating a second telemetry lifecycle before that boundary is designed.

The compiled runtime exposes `harnest.logging` and `harnest.tracing` to authored
code and owns the default OpenTelemetry bootstrap. Managed ADK and LangGraph use
one process-global Harnest provider when OTLP is enabled. Advanced ADK may
initialize its own global providers while creating the official FastAPI
surface, so Harnest adopts those providers and does not add a duplicate
environment exporter. Externally installed global providers are adopted without
shutdown; explicitly authored telemetry exporters are added to providers that
support processors.

Every execution transport creates a low-cardinality
`harnest.agent.invoke` span. SSE spans remain open for the generator lifetime;
each `/live` `response.create` frame gets a separate invocation span. FastAPI
instrumentation propagates incoming W3C context. Agent logs use the standard
Python logging namespace `harnest.agent.*`, JSON console formatting, immutable
bound fields, and OpenTelemetry trace correlation. Providers are flushed at the
server boundary, never per request.

Prompt text, response text, metadata, credentials, headers, and raw session IDs
are excluded from Harnest spans and logs. Scaffolds set GenAI and ADK content
capture off. Collector credentials remain engine-resolved secrets.

Client-tool request and result-submission transitions emit agent/user-triggered
audit events through the same OTEL logging pipeline. Only the declared tool
name crosses that boundary; arguments, results, principal IDs, and request IDs
are excluded.

The native ADK eval lane prepends an eval-only event filter before authored plugins.
It removes parts marked as model thoughts using the same customer-facing rule
as the neutral runtime, then lets the official ADK evaluator score the remaining
visible text and tool trajectory. This compensates for ADK response matchers
that otherwise concatenate every text part, including hidden reasoning.
The LangGraph lane instead scores the neutral runtime's already-public final
response and translates its canonical tool events into the same evaluator input.
Each eval suite emits a balanced user-triggered start/finish audit event.
Closed pytest-capture handlers are detached before ADK runs; handlers installed
by evaluation dependencies are removed and closed at lane completion. This
prevents native plugin teardown logs from writing to a closed capture stream
while retaining Harnest's OTEL audit path.
