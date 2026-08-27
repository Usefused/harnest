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

## Filesystem-first composition

An agent folder is itself the Python authoring unit; there is no required wrapper
package. Its `agent.py` creates only the root definition:

```python
from harnest.agent import Agent

root_agent = Agent(...)
```

That `Agent` is the supported one-agent managed form. Harnest builds an ADK
`LlmAgent` or a LangGraph/LangChain tool-loop agent depending on
`spec.framework.name`.

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
value or `Event(output=..., route=..., message=...)`; routed edges compare
against the event route. A `Join` is the explicit fan-in marker for incoming
branches. `max_concurrency`, when set, is passed to the backend workflow.

The ADK backend lowers this IR to `google.adk.workflow.Workflow`, `Edge`, and
`JoinNode`. The LangGraph backend lowers it to a `StateGraph` with message,
value, and route state, an in-memory checkpointer, conditional edges, and
LangGraph joins. Nested graph cycles and framework-invalid native nodes are
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
owns framework composition, lifecycle, persistence, and provider-specific
dependencies.
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
so agents do not declare Harnest in `requirements.txt`. Normal standard-library
and third-party imports remain valid. Import-free source fails compilation;
there is no implicit-global compatibility bridge. The bundle always requires a
non-empty UTF-8 sibling `instructions.md`. Managed `Agent` composition supplies
it when the definition omits `instruction`; an explicit nonblank instruction
wins without merging. For a managed `Graph` or advanced application the file
remains required bundle metadata, but Harnest does not inject it into the
framework object. Managed compilation discovers resources relative to an owning
`agent.py`. Paths in this table are relative to that composition folder unless
marked root-only:

| Path | Required filename-matched export |
| --- | --- |
| `tools/<name>.py` | An `@tool`-decorated callable named `<name>`. |
| `subagents/<name>.py` | Exactly one `AgentDefinition` named `<name>`. |
| `mcp/<name>.py` | An `MCPClient` or `None` named `<name>`. |
| `plugins/<name>/mcp/<client>.py` (root-only) | A plugin-owned `MCPClient` or `None` named `<client>`. |
| `plugins/<name>/skills/<skill>/SKILL.md` (root-only) | Plugin-owned progressive skills. |
| `extensions/<name>/lifecycle.py` (root-only) | One portable `Extension` exported as `extension`. |
| `extensions/<name>/adk.py` (root-only) | Optional native ADK plugin integration. |
| `extensions/<name>/langgraph.py` (root-only) | Optional native LangGraph middleware integration. |
| `sandbox/sandbox.py` | One optional ADK-only `Sandbox` defining the agent's lazy code-execution backend. |
| `skills/<kebab-name>/SKILL.md` | A progressive skill whose frontmatter `name` matches its directory. |
| `evals/<id>.evalset.json` (root test lane) | A test-only, ADK-only `EvalSet` whose `eval_set_id` matches its filename. |
| `tests/unit/test_*.py` | Offline agent/tool tests run by `harnest test`. |
| `tests/smoke/test_*.py` | Explicitly enabled live-runtime tests. |

Every optional resource root may be missing, empty, or contain only ignored
files. In those cases compilation moves on without creating a runtime resource.
Once a public entry exists, its complete convention is enforced; populated but
invalid resources are never silently skipped.

`None` is an intentional MCP state, not an error. Optional integrations should
check all required environment variables in their definition module and export
`None` when configuration is incomplete. This keeps importing, local runs, and
deployments safe from half-configured connections. Enabled definitions should
retain `${ENV_VAR}` placeholders rather than interpolating credentials;
`MCPClient` expands them only when constructing the selected framework's MCP
connection. ADK creates `McpToolset`; LangGraph uses
`langchain-mcp-adapters` and defers asynchronous tool discovery until runtime.
The descriptor supports `stdio`, modern `streamable-http`, and legacy `sse`.
ADK receives the matching connection-parameter type; LangGraph receives the
matching `langchain-mcp-adapters` connection dictionary. HTTP request timeouts
and long-lived SSE idle-read timeouts are separate so quiet legacy servers do
not disconnect at the normal request deadline.

The compiler ignores `__init__.py`, dotfiles, cache directories, and files whose
names start with `_`. It imports public resource files in deterministic filename
order. Missing or wrongly typed exports are convention errors; module import
failures are reported with the resource path; duplicate tool names, subagent
names, or identical MCP configurations fail rather than silently shadowing.
Resources explicitly present on the root definition are kept first, followed by
direct discovered resources and plugins in sorted plugin-name order. Plugins
contribute only MCP clients and progressive skills: the clients expose tools,
while the skills teach the host agent when and how to use those tools. A plugin
must contain at least one of each and never contributes an agent or execution
path. See [Plugins](plugins.md).

Runtime extensions are discovered separately in sorted directory order. Each
`extensions/<name>/lifecycle.py` exports a portable `Extension` named
`extension`. Optional `adk.py` and `langgraph.py` files add native integration
for the selected framework without requiring the other backend. Portable and
native pieces are additive: portable behavior surrounds the neutral runtime,
while native behavior runs inside the framework. See [Runtime
extensions](extensions.md).

Advanced mode continues to own lifecycle wiring in `agent.py`. Managed
LangGraph also rejects ADK sandbox executors, `output_key`,
`generate_content_config`, and implicit subagent delegation rather than
silently ignoring them.

Skills expose progressive list, load, and resource tools instead of
placing every skill body in the root prompt. ADK uses its standard loader and
one `SkillToolset`; LangGraph receives portable callable tools providing the
same progressive access. A skill may contain Agent-Skills-style
`references/`, `assets/`, and `scripts/` content. Public entries directly under
`skills/` must be skill directories; symlinks are rejected.

The optional `evals/test_config.json` is an ADK `EvalConfig`. Eval sets are
sorted and validated during compilation, including unique case IDs, but remain
test-only and never become instructions or runtime tools. `discover_evals()`
also exposes the validated paths for tooling. `harnest test <agent-folder>
--evals` runs the unit suite first, then evaluates every validated eval set in
deterministic filename order through ADK's official evaluator, automatically
applying the optional config. Authors never address temporary compiled paths.
Eval execution is explicit because it can invoke live models and consume
credentials, time, or paid capacity. Harnest passes `num_runs=1` to ADK's
evaluator so a single command does not silently double model usage. The compiled
artifact is temporary and no ADK eval history is persisted; external CI is
responsible for retaining output. Selecting `--evals` without at least one
validated eval set is a convention error.

The test runner discovers and executes eval assets only from the root bundle's
`evals/`. Nested eval files can be reached by composition validation, but they
are not a nested-agent test lane and are never selected by `harnest test
--evals`.

The `--evals` lane is currently rejected when `framework.name` is `langgraph`;
unit and opt-in smoke tests remain available through the neutral runtime.

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

Unit tests are an offline contract: they should exercise definitions and local
tool functions without calling a model, MCP server, or HTTP endpoint. Harnest
reinforces the boundary by withholding HTTP/smoke fixtures, but it does not
install an operating-system network sandbox. Smoke tests are always opt-in
because they can use live models, credentials, MCP services, time, and money.
Their environment must be supplied explicitly by the developer or CI job.

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

Filesystem placement is the access model. There are no tool/skill name lists on
`Agent` that select access to discovered folders, and no separate `SubAgent`
class. An `Agent` becomes a nested subagent through its folder and its
graph/parent relationship.

Compilation writes a separate runtime directory containing the preserved source
tree, generated `agent.py`, `__init__.py`, and `__main__.py` adapters, the
`harnest-agent` launcher, and `harnest-manifest.json`. This output is the
selected framework's runtime package. Generated `agent.py` exports the neutral
`CompiledApplication` as `application`, the provider application as `app`, and
the provider target as `root_agent`; its manifest records `framework.name` and
the effective mode. Source does not need `__init__.py` and is loaded through the
generated adapter. Generated `.harnest/`
content is disposable and must not be edited or committed. VCS data, virtual
environments, caches, `.adk/`, `.harnest/`, `.env` files, and bytecode are
excluded. Source symlinks are rejected, keeping artifacts self-contained and
preventing credentials or external files from being pulled in accidentally.

The artifact can be served without the Go provisioner. Harnest's primary public
surface is deliberately transport- and provider-neutral:

| Route | Contract |
| --- | --- |
| `GET /agent` | Returns `{id,name,description,card,endpoints}` for the compiled agent. |
| `POST /sessions` | Create an in-memory session from `{id?,state?}`; returns 201 with `{id,state}`. |
| `GET /sessions`, `GET /sessions/{id}` | Return `{sessions:[{id,state},...]}` or one `{id,state}`. |
| `PATCH /sessions/{id}` | Apply the exact `{"stateDelta": {...}}` body. |
| `DELETE /sessions/{id}` | Delete a session and return 204. |
| `POST /responses` | Run `input` against an optional `sessionId`; return neutral JSON, or named SSE when `stream` is true. |
| `WS /live` | Direct multi-turn WebSocket transport using neutral connect, request, and response event frames. |

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

The first `/live` client frame is `{"type":"connect","sessionId":"..."}`
with an optional session ID. The server replies with `session.connected`.
Subsequent client frames are `response.create` with non-empty `input` and
optional `requestId`/`metadata`, or `session.close`. Server response events use
the SSE event names and fields and echo `requestId`. Invalid frames and execution
failures use a typed `error` frame; policy violations close with WebSocket code
1008. There is no mode flag or protocol rerouting.

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
`GET /healthz` and `GET /.well-known/agent-card.json` remain available for
health and card discovery in both frameworks.

This is a process boundary, not a deployment boundary: the interpreter still
needs Harnest, the selected framework, model adapters, and agent dependencies.
The standalone server does not interpret deployment resources, resolve secrets, enforce
permissions, scale replicas, or add authentication, tenant isolation, and TLS.
The neutral runtime has one session authority per backend driver. LangGraph
stores returned graph state directly and does not install a second checkpointer;
ADK uses the neutral driver's owned runner. In advanced mode, official ADK
routes intentionally use ADK's separate native session namespace. Production
exposure requires a trusted authenticated gateway. Those platform concerns
remain the provisioner/engine's responsibility.

The serving path is deliberately one-way:

```text
compiled application -> ADKRuntimeDriver | LangGraphRuntimeDriver
                     -> one neutral FastAPI router
                     -> /agent /sessions /responses /live
```

Only drivers translate native sessions, inputs, and events. Validation,
deadlines, concurrency, JSON envelopes, SSE sequencing, WebSocket framing, and
shutdown behavior are implemented once by the neutral router.

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
| `evals/*.evalset.json` | Agent owner | ADK eval CLI and CI | ADK-only test conversations and expected behavior; never deployed as capabilities. |

The schemas in `schemas/` are the editor and CI contract. The Go loader also
uses strict YAML decoding and runtime checks. Schema defaults are documentation;
the engine must apply defaults explicitly rather than assuming a validator
mutated the manifests.

The engine should inject `spec.environment` and resolved `spec.secrets`, enforce
the declared network/filesystem permissions, install the requirements file under
the requested Python version, invoke `harnest compile` with the configured
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
direct framework imports do not relax this rule. Agent `requirements.txt`
constraints may narrow the supported range, but must not widen it beyond the
installed Harnest release. The exact framework version and Harnest version are
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
entrypoint module, and optional requirements file must then validate. Sources are
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

- create the requested Python environment and install `requirementsFile`;
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
exporter. Externally installed global providers are adopted without mutation or
shutdown.

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

The ADK eval lane prepends an eval-only event filter before authored plugins.
It removes parts marked as model thoughts using the same customer-facing rule
as the neutral runtime, then lets the official ADK evaluator score the remaining
visible text and tool trajectory. This compensates for ADK response matchers
that otherwise concatenate every text part, including hidden reasoning.
