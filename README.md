# Harnest

Harnest is a filesystem-first compiler and standalone runtime for self-serve
agents. Its managed authoring layer supports Google ADK and LangGraph, lowers a
small graph API to the selected framework, and serves both through the same
HTTP/WebSocket contract. Agent authors put instructions, tools, plugins,
extensions, subagents, MCP connections, skills, evals, and tests in conventional folders;
the native CLI validates that source, compiles it, and runs the result.

The design borrows the useful part of Eve's developer experience: one agent is
one inspectable directory, and optional capabilities are added conventionally as
the agent grows.

## Install

GitHub Releases ship a native `harnest` CLI with its matching Python wheel
and a native `uv` bootstrapper embedded directly in the executable. Installation
does not require a preinstalled Python: when no compatible interpreter exists,
Harnest installs a pinned managed CPython into its own data directory. Normal
CLI commands use the isolated managed environment afterward.
On macOS or Linux, install the latest release into an isolated managed runtime:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/creativeJoe007/harnest/main/install.sh |
  sh
```

The installer displays the selected release and destination paths, then asks
for confirmation before downloading or changing the system. For an explicitly
authorized non-interactive installation, set `HARNEST_YES=1` on the `sh`
process:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/creativeJoe007/harnest/main/install.sh |
  HARNEST_YES=1 sh
```

The binary is installed at `${HARNEST_INSTALL_DIR:-$HOME/.local/bin}/harnest`;
Python dependencies stay in the dedicated
`${HARNEST_RUNTIME_DIR:-$HOME/.harnest/runtime}` virtual environment. Downloads
must match the release's published SHA-256 checksum before anything is
installed. The installer uses an existing Python 3.10+ when possible and falls
back to managed CPython 3.12 through its embedded `uv`; set
`HARNEST_BOOTSTRAP_PYTHON` only to require an exact host interpreter. The
managed runtime installs both supported compiler backends;
model-provider and agent-specific packages remain declared by each agent. Pin
or redirect the source with `HARNEST_VERSION` and
`HARNEST_REPO=owner/repository`. See [Installation and
releases](docs/releases.md) for verification, private-repository, upgrade, and
maintainer instructions.

Create and exercise an agent with compiler-owned authoring imports:

```bash
harnest skills install
harnest init support-agent
harnest test support-agent
harnest compile support-agent --output .harnest/support-agent
harnest serve support-agent
```

The runtime directory is internal to Harnest. Do not activate it or invoke its
Python modules directly; the native `harnest` CLI selects it automatically. The
installer replaces Harnest's retired Python launcher in place when possible. If
`harnest --version` still does not print `harnest version ...`, run
`type -a harnest` to identify a non-writable or unrelated command collision.

## Coding-agent authoring skill

Harnest embeds a separate `harnest-authoring` Agent Skill for coding agents that
create or modify Harnest projects. It documents every source folder, the
compiler-provided `harnest.*` Python namespaces, managed/advanced and ADK/LangGraph
boundaries, and the correct test/compile/serve workflow.

Install it into the current project for the invoking coding agent:

```bash
harnest skills install
```

Auto-detection recognizes active Codex, Claude Code, Cursor, and GitHub Copilot
environments. When no agent identifies itself, Harnest uses the portable Agent
Skills location `.agents/skills/harnest-authoring/`. Select a target or project
explicitly when needed:

```bash
harnest skills install --target claude --project ./support-agent
harnest skills install --target cursor --project ./support-agent
harnest skills install --target copilot --project ./support-agent
harnest skills install --target agents --project ./support-agent
```

Targets install beneath `.agents/skills`, `.claude/skills`, `.cursor/skills`, or
`.github/skills` as appropriate. Existing content is preserved unless
`--force` is supplied. `harnest skills show` prints the embedded entrypoint.
This authoring skill is never placed in the generated agent's runtime `skills/`
folder and is never compiled into the deployed agent.

The skill includes a folder-editing playbook that tells coding agents how to
identify the owning `agent.py`, choose the correct resource folder, preserve
existing work during moves or promotions, and validate managed versus advanced
mode changes. After upgrading Harnest, reinstall with `--force` to receive the
new bundled guide only after confirming the project-local copy has not been
customized:

```bash
harnest skills install --force
```

## Repository layout

```text
cmd/harnest/                         Native Cobra compiler CLI
src/harnest/                         Python compiler and agent runtime
schemas/                             Config, card, and plan JSON Schemas
examples/self-serve/
└── agents/
    └── helpdesk/
        ├── config.yaml              Compute, scaling, env, secrets, permissions
        ├── agent-card.yaml          A2A 1.0 discovery metadata
        ├── agent.py                 Root definition using harnest.* imports
        ├── instructions.md           Required root instructions
        ├── requirements.txt
        ├── tools/                    Decorated callable exports
        ├── subagents/                AgentDefinition exports
        ├── mcp/                      MCPClient connections
        ├── plugins/                  MCP-client-and-skill capability bundles
        ├── extensions/               Portable and native runtime lifecycle
        ├── sandbox/                  ADK-only code-execution backend
        ├── skills/                   Portable progressive skills
        ├── evals/                    ADK-only eval sets and config
        └── tests/
            ├── unit/test_*.py        Offline behavior tests
            └── smoke/test_*.py       Opt-in live-model tests
```

## Python authoring API

Every `config.yaml` selects the compiler backend and authoring mode:

```yaml
apiVersion: harnest.dev/v1alpha1
kind: Agent
spec:
  entrypoint: agent:root_agent
  framework:
    name: langgraph       # adk or langgraph
    mode: managed         # managed (default) or advanced
  # runtime, resources, and the remaining deployment fields follow
```

## Framework version compatibility

Framework support is release-bound. Each Harnest release declares the ADK and
LangGraph version ranges it can compile and run; the current ranges are the
`google-adk` and `langgraph` constraints published in `pyproject.toml` and in
newly generated `requirements.txt` files. The compiler checks the installed
framework distribution before loading authored agent code and fails when its
version falls outside the selected Harnest release's range.

This contract applies equally to managed and advanced mode. Advanced authors
still import ADK or LangGraph directly, but direct imports are an authoring
escape hatch rather than a way to bypass Harnest's tested runtime boundary. To
adopt a newer unsupported framework release, upgrade Harnest to a release that
declares support for it, then update the agent dependency range and run its
tests. Do not widen the agent's framework constraint independently and assume
compatibility.

Compiled manifests record the Harnest version and the installed selected
framework version, alongside the effective framework name and mode. A deployed
artifact therefore carries the exact compatibility context it was compiled
with.

In `managed` mode, the entrypoint exports either a provider-neutral
`harnest.graph.Graph` or an `Agent`. `Graph` is the primary, graph-like managed
API; an `Agent` remains the supported concise one-agent form. The
compiler discovers sibling resources and lowers the definition to the selected
framework.

This graph runs a normalizer, routes its `Event` output, and then invokes a
filesystem-discovered subagent named `order_specialist`:

```python
from harnest.graph import START, Edge, Event, Graph, Join


def normalize(request):
    return Event(output=request.strip(), route="ready")


root_agent = Graph(
    name="support_flow",
    nodes={
        "normalize": normalize,
        "specialist": "order_specialist",
        "complete": Join(),
    },
    edges=(
        Edge(START, "normalize"),
        Edge("normalize", "specialist", route="ready"),
        Edge("specialist", "complete"),
    ),
    max_concurrency=4,
)
```

`Graph.nodes` is a mapping of stable Python identifiers to `Agent` definitions,
single-input callables, nested `Graph` values, `Join()` markers, supported
backend-native nodes, or strings naming discovered tools/subagents. `Edge.route` accepts a
boolean, integer, string, or a non-empty sequence of those values. A callable
may return a plain downstream value or `Event(output=..., route=...,
message=...)`; `message` emits assistant text. `START` marks entry edges and a
`Join` waits for all incoming branches. Graph construction rejects unknown edge
references, duplicate edges, and unreachable nodes before either backend runs.

The one-agent managed form stays intentionally small:

```python
import os

from harnest.agent import Agent
from harnest.model import LiteLLMModel


root_agent = Agent(
    name="support",
    model=LiteLLMModel(
        model=os.getenv("LITELLM_MODEL", "ollama_chat/qwen3.5:cloud"),
        api_base=os.getenv("LITELLM_API_BASE", "http://127.0.0.1:11434"),
        thinking=True,
    ),
    description="Answers product questions and triages support requests.",
)
```

For ADK this builds an `LlmAgent`; for LangGraph it builds a LangChain tool-loop
graph. The Harnest runtime remains the single session authority for both.
`LiteLLMModel` and `OllamaModel` have
adapters for both frameworks. Provider-specific Python dependencies still
belong in the agent's `requirements.txt`.

Both connectors support thinking and non-thinking models. Set `thinking=True`
to request reasoning, `thinking=False` to disable it, or omit the option to use
the provider default. For an exact provider-supported level, pass
`reasoning_effort="low"`, `"medium"`, or `"high"` instead of `thinking`.
Harnest preserves framework/model reasoning state for subsequent turns but
never places hidden thought parts in `/responses`, SSE, `/live`, or eval output.
If a provider completes with reasoning only and no visible answer or structured
result, JSON returns `502` and streaming transports emit an `error` event rather
than reporting a successful empty response.

`advanced` mode is the framework-native escape hatch. It still exports the
public `Agent` type, constructed with `Agent.advanced(...)`. Harnest validates
an ADK `App`/`BaseAgent`/`BaseNode` or a compiled LangGraph `Pregel` and retains
the neutral server boundary. ADK normally needs no I/O adapter. LangGraph
defaults to conventional `messages` state; custom state uses `input_adapter`
and `output_adapter`:

```python
from harnest.agent import Agent


root_agent = Agent.advanced(
    target=compiled_graph,
    name="support",
    input_adapter=lambda text, metadata: {"ticket": text, "meta": metadata},
    output_adapter=lambda state: state["answer"],
)
```

Import ADK or LangGraph directly to construct `target`; Harnest does not wrap or
re-export either framework. Ordinary `harnest.graph.Graph` construction stays
in managed mode—advanced mode is only for framework behavior that the portable
API cannot express.

Advanced mode does not perform managed filesystem composition: the authored
framework application owns its tools, MCP clients, subagents, prompts,
checkpointer, and lifecycle behavior.

To assess an existing edited project without rewriting any source, run:

```bash
harnest mode advanced support-agent --check
```

The check is read-only: it reports the current framework, mode, entrypoint, and
managed folders that need explicit framework wiring. It never regenerates
`agent.py`, moves resources, changes `config.yaml`, or discards user
modifications. Use
`harnest init support-agent --mode advanced` only when creating a new project;
migrate existing code deliberately after reviewing the report.

These imports are supplied by the compiler/runtime and do not make Harnest an
agent-owned dependency: do not add `harnest` to `requirements.txt`. Authored
resources import only the symbol they define, such as
`from harnest.tool import tool` or `from harnest.mcp import MCPClient`; they
still never import or register sibling tools, subagents, MCP definitions,
instructions, or skills. Every bundle requires sibling `instructions.md`.
Managed `Agent` composition reads it as the default prompt; managed `Graph` and
advanced applications retain it as bundle metadata. Managed compilation loads
public Python files from `tools/`, `subagents/`, and `mcp/`. Each filename is
its export contract: `tools/lookup_order.py` must
export an `@tool` callable named `lookup_order`;
`subagents/order_specialist.py` must export an `AgentDefinition` named
`order_specialist`; and `mcp/catalog.py` must export an `MCPClient` (or
`None`) named `catalog`.

### Folder-scoped agent ownership

In managed mode, each folder-based `agent.py` is a composition boundary. The
root `agent.py` owns resources in the root folder. A nested
`subagents/researcher/agent.py` owns the supported resource folders beside it:
`instructions.md`, `tools/`, `skills/`, `mcp/`, and `sandbox/`. ADK nested
agents may also own child agents under their sibling `subagents/`; LangGraph
nested `Agent` definitions cannot consume discovered child subagents today.
Root tools and skills are not inherited by that nested agent, and its private
resources are not exposed to its parent merely because the folders are nested.

A flat `subagents/researcher.py` is useful when only an agent definition is
needed. It cannot own private resource folders or a private `instructions.md`;
move it to `subagents/researcher/agent.py` when it needs either. An `Agent`
written inline as a node in the root `agent.py` remains root-scoped and uses the
root folder's discovered resources.

Location is the access declaration. Do not repeat discovered tool or skill
names in `Agent` fields as folder-access selectors. Harnest has no separate
`SubAgent` class: the same `Agent` becomes a nested subagent through its folder
and graph/parent reference. Plugins and extensions are deliberately root-only;
a populated nested `plugins/` or `extensions/` folder fails compilation.

`plugins/<name>/` packages MCP client connections with the progressive skills
that teach the host agent when and how to use their tools. Its `mcp/` files
follow the normal MCP client export convention and its `skills/` folders follow
the Agent Skills layout. A non-empty plugin requires both halves; plugins never
contain agents or lifecycle behavior.
See the [plugin contract](docs/plugins.md).

`extensions/<name>/lifecycle.py` exports an `Extension` named `extension` for
portable request and output lifecycle behavior. Optional `adk.py` or
`langgraph.py` files provide tighter native integration for the selected
framework. Use extensions for persistence, guardrails, auditing, and similar
runtime concerns. See the [runtime extension contract](docs/extensions.md).

An optional `sandbox/sandbox.py` exports one `Sandbox`. Built-in container
sandboxes deny network access by default; provider packages can supply another
ADK executor. See the [sandbox contract](docs/sandbox.md) for the exact security
boundary and dependency requirements. Managed LangGraph compilation rejects
unsupported sandbox configuration instead of silently dropping it.

Every optional resource directory may be absent or empty. Empty directories are
ignored during compilation; once a public resource is present, its filename and
export contract are validated strictly. `harnest init` creates starter content
for every scaffolded resource directory so a new project demonstrates each
convention immediately.

Authored files are compiler input. Their `harnest.*` imports resolve while the
compiler or compiled runtime is active; the agent's provider-specific
requirements remain separate. Use `harnest compile` or `harnest serve`; both
load the generated package through the managed runtime. Import-free authored
modules are rejected: every Harnest authoring symbol must be imported explicitly.

Compile source into a disposable runtime artifact:

```bash
harnest compile examples/self-serve/agents/helpdesk \
  --output .harnest/helpdesk
```

The output contains `source/`, generated `agent.py`, `__init__.py`, and
`__main__.py` adapters, a `harnest-agent` launcher, and
`harnest-manifest.json`. Generated `agent.py` exports a neutral
`CompiledApplication` as `application`, the selected provider object as `app`,
and the selected target as `root_agent`. The artifact is independently
serveable,
but remains build output rather than authored source; `.harnest/` is ignored and
should be regenerated instead of edited or committed. VCS metadata, virtual
environments, caches, local `.env` files, and bytecode are excluded rather than
baked into the artifact.

Folders under `skills/` use the Agent Skills directory format:
`skills/<kebab-name>/SKILL.md` must have YAML frontmatter whose `name` matches
the directory. ADK uses its standard skill toolset; LangGraph receives portable
progressive list/load/resource tools. Eval assets are currently ADK-only and
test-only: `evals/<eval-set-id>.evalset.json` contains
an official ADK `EvalSet`, and optional `evals/test_config.json` contains its
`EvalConfig`.

Agent-owned Python tests are also convention-based and import-free. Run the
offline suite with:

```bash
harnest test examples/self-serve/agents/helpdesk
```

This compiles the agent, then collects only `tests/unit/test_*.py`. Unit tests
receive `agent` (the selected framework target) and `tools` (a read-only mapping
of local tool names to callables). They may import standard-library and third-party
packages, but never import Harnest or manually load the compiled artifact. Unit
tests are expected to avoid model, MCP, HTTP, and other network calls. This is a
test convention rather than an operating-system network sandbox.

Live checks belong under `tests/smoke/test_*.py` and run only when explicitly
requested:

```bash
harnest test examples/self-serve/agents/helpdesk --smoke
```

`--smoke` runs unit tests first, then smoke tests. Smoke tests also receive a raw
FastAPI `client` and a higher-level `smoke` fixture. Use
`smoke.respond(input, session_id=None, metadata=None)` for one neutral JSON
response, or `smoke.stream(...)` for the ordered neutral SSE data objects. The
command exits nonzero if compilation, collection, unit tests, or opted-in smoke
tests fail. Smoke tests may consume live model and MCP credentials, so CI should
enable them deliberately and inject the same runtime environment as the agent.

For an ADK agent, run all validated official ADK eval sets after the unit suite
with:

```bash
harnest test examples/self-serve/agents/helpdesk --evals
```

The runner discovers sorted `evals/*.evalset.json` files, applies
`evals/test_config.json` when present, and stops at the first failing lane. Evals
are opt-in because they normally invoke the configured model and may consume
credentials, time, and paid capacity. Use `--smoke --evals` for the full run:
unit and smoke tests first, then all eval sets. Compilation and path handling are
internal to `harnest test`; authors do not need to invoke ADK's evaluator CLI or
reference generated artifact paths. Harnest fixes ADK evaluation to one run per
case to avoid an implicit duplicate model charge. The compiled artifact is
temporary and eval history is not persisted; CI should retain command output as
its test record. Before ADK scores a response, Harnest removes parts marked as
model thoughts using the same customer-facing rule as `/responses`; visible
text and tool-call/tool-result events remain available to evaluation metrics.
`--evals` requires at least one validated eval-set file and is rejected for the
LangGraph backend.

The eval runner discovers only the root agent folder's `evals/`. Eval files
placed below a nested subagent may be encountered by compilation validation but
are not selected or executed by `harnest test --evals`; keep runnable eval sets
at the root.

Both test trees are test-only. They are not added to prompts, tools, subagents,
skills, the Agent Card, or the deployed capability surface.

The `skills` advertised in `agent-card.yaml` are public A2A capabilities;
`skills/` contains internal progressive instruction packs. They need not be one-to-one,
but the card must truthfully describe what the composed runtime can do.

Tools and MCP descriptors are portable: ADK receives function tools and
`McpToolset` instances, while LangGraph receives LangChain tools and uses
`langchain-mcp-adapters` for configured MCP clients. ADK can implicitly attach
filesystem subagents to a root `Agent`; LangGraph requires those agents to be
referenced explicitly from a `Graph`, avoiding accidental model-controlled
delegation. ADK-specific `output_key`, `generate_content_config`, sandbox
executors, and official evals are rejected by LangGraph rather than ignored.

Remote MCP clients support both current Streamable HTTP and the legacy SSE
transport. Point an SSE client at the server's SSE endpoint, commonly `/sse`;
the underlying MCP protocol discovers the separate client-to-server message
endpoint from that stream:

```python
from harnest.mcp import MCPClient


modern = MCPClient.streamable_http(
    "${CATALOG_MCP_URL}",
    headers={"Authorization": "Bearer ${CATALOG_MCP_TOKEN}"},
)

legacy = MCPClient.sse(
    "${LEGACY_MCP_URL}/sse",
    headers={"Authorization": "Bearer ${LEGACY_MCP_TOKEN}"},
    timeout_seconds=10,
    sse_read_timeout_seconds=600,
)
```

`timeout_seconds` bounds connection and MCP operation work;
`sse_read_timeout_seconds` independently controls how long an idle SSE stream
may remain quiet. The default is five minutes for both ADK and LangGraph.

## Folder contract

Every deployable directory must contain:

- `config.yaml`: deployment and compiler selection, including
  `spec.framework.name` (`adk` or `langgraph`), optional mode (`managed` by
  default or `advanced`), resources, scaling,
  environment, secret references, and permissions. Secret values are never
  allowed; `secretRef` points at the engine's secret provider.
- `agent-card.yaml`: the public A2A 1.0-facing description, interfaces,
  modalities, capabilities, and skills supported by the current runtime.
- the Python source module named by `spec.entrypoint` using `module:symbol`
  syntax. Managed mode exports an `Agent` or `Graph`; advanced mode exports an
  `Agent` created with `Agent.advanced(...)`. The compiler turns it into the
  runtime module.
- optional sibling `tools/`, `subagents/`, `mcp/`, and `sandbox/` directories
  whose public files follow the filename-matched export conventions above;
  root-only `plugins/<name>/{mcp,skills}` capability bundles; and root-only
  `extensions/<name>/` runtime lifecycle directories. Folder-based nested
  subagents get their own supported sibling resource scope as described above.
- non-empty UTF-8 `instructions.md` (also required as bundle metadata in advanced
  mode), plus optional `skills/`, ADK-only `evals/`, and
  test-only `tests/unit/` and `tests/smoke/` directories following the
  conventions above.
- the declared requirements file, if any.

Unknown YAML fields fail validation. Compilation is deterministic and ignores
virtual environments and caches when hashing or copying source.

Agent-owned Python files use explicit `harnest.*` authoring imports alongside
standard-library and third-party imports. The compiler/runtime supplies the
Harnest namespace, so it is not listed in the agent's `requirements.txt`.

See the [self-serve walkthrough](examples/self-serve/README.md), the fully
composed [agent example](examples/self-serve/agents/helpdesk/agent.py),
and the [architecture notes](docs/architecture.md).

## Run the example locally

The helpdesk example defaults to LiteLLM plus Ollama's tool-capable
`qwen3.5:cloud` model, so the agent process needs no Gemini key or direct Ollama
API key. The local Ollama daemon brokers cloud authentication. With Ollama
installed, its service running, and `ollama signin` completed:

```bash
make example-install
make live-run
```

`live-run` first compiles the example's managed ADK source into
`.harnest/helpdesk`, then starts ADK's
interactive terminal runner against that generated artifact. Override either setting
without changing agent code, for example:

```bash
make live-run LITELLM_API_BASE=http://127.0.0.1:11434 \
  LITELLM_MODEL=ollama_chat/qwen3.5:9b
```

Run `make example-test` for the offline agent-owned unit suite. For a
non-interactive end-to-end check that requires a real Ollama model/tool round
trip, run `make example-smoke` (or the `make live-test` alias). It succeeds only
after the model calls the example's `triage_request` tool and returns the
expected synthetic triage result.

The optional knowledge-base MCP server stays disabled locally unless
`KNOWLEDGE_MCP_TOKEN` is set. For a container deployment, change
`LITELLM_API_BASE` and the matching outbound permission to the reachable Ollama
service; container loopback only works when Ollama is a sidecar.

## Serve the compiled agent

The compiled folder includes a small standalone HTTP launcher. After installing
the Python dependencies and signing Ollama in, start it in one terminal:

```bash
harnest serve examples/self-serve/agents/helpdesk
```

It listens on `127.0.0.1:8080` by default. Override the binding with
`SERVE_HOST` and `SERVE_PORT`; `SERVE_MAX_CONCURRENCY` caps concurrent work and
`SERVE_REQUEST_TIMEOUT` is the non-streaming response deadline. Or invoke the
generated launcher directly:

```bash
.harnest/helpdesk/harnest-agent --host 127.0.0.1 --port 8080
```

The primary standalone API is Harnest's transport-neutral contract. Inspect the
agent, create an in-memory session, then run a turn from another terminal:

```bash
curl -sS http://127.0.0.1:8080/agent

curl -sS -X POST http://127.0.0.1:8080/sessions \
  -H 'Content-Type: application/json' \
  --data '{"id":"demo-session","state":{"channel":"demo"}}'

curl -sS -X POST http://127.0.0.1:8080/responses \
  -H 'Content-Type: application/json' \
  --data '{"input":"Triage a fictional production API authentication outage.","sessionId":"demo-session","metadata":{"source":"readme"}}'
```

`GET /agent` returns the compiled agent's identity, copied Agent Card, and route
links. `POST /responses` accepts `input`, optional `sessionId`, optional
`metadata`, and optional boolean `stream`. Without a session ID it creates one.
The JSON response has this stable, provider-neutral shape:

```json
{
  "id": "resp_...",
  "status": "completed",
  "sessionId": "demo-session",
  "outputText": "...",
  "output": [
    {
      "type": "message",
      "role": "assistant",
      "content": [{"type": "output_text", "text": "..."}]
    }
  ],
  "metadata": {"source": "readme"}
}
```

Tool calls and results appear in `output` as ordered `tool_call` and
`tool_result` items. Reuse the session ID for conversational continuity. Session
CRUD is `GET /sessions`, `GET /sessions/{id}`, `PATCH /sessions/{id}` with the
exact body `{"stateDelta": {...}}`, and `DELETE /sessions/{id}` (204). Sessions
are process-local and disappear when the launcher stops.

Set `stream: true` on the same endpoint for named Server-Sent Events:

```bash
curl -N -sS -X POST http://127.0.0.1:8080/responses \
  -H 'Content-Type: application/json' \
  --data '{"input":"What should I collect next?","sessionId":"demo-session","stream":true}'
```

The stream starts with `response.created`, may emit `response.text.delta`,
`response.tool_call`, and `response.tool_result`, and ends with
`response.completed`. Every data object carries `type`, an increasing `sequence`,
`responseId`, and `sessionId`; delta events add `delta`, tool calls add
`id`/`name`/`arguments`, tool results add `callId`/`name`/`output`, and the final
event carries `status`, `outputText`, ordered `output`, and `metadata`. A failure
after streaming begins is a terminal named `error` event.

`WS /live` is the direct neutral WebSocket API. The first client frame is
`{"type":"connect","sessionId":"demo-session"}`; omit `sessionId` to create
one. After `session.connected`, send
`{"type":"response.create","input":"...","requestId":"optional","metadata":{}}`.
The server emits the same `response.*` event types, echoing `requestId`, and
accepts `{"type":"session.close"}`. There is no mode flag or HTTP-to-WebSocket
rerouting.

The ADK and LangGraph runtime adapters map provider events into the same neutral
message, tool-call, and tool-result items. Provider names, model versions,
reasoning details, and framework bookkeeping are not exposed. Before a stream
starts, request errors use FastAPI's JSON `{"detail":"..."}` shape with normal
HTTP status codes; SSE and WebSocket execution errors use a typed `error` event.
The server has no authentication or tenant boundary, so put authentication and
TLS in front of any non-loopback deployment.

Equivalent helpers are `make demo-agent`, `make demo-session`,
`make demo-response`, and `make demo-stream`; choose a new session with
`DEMO_SESSION_ID=...` when needed. Health and card discovery remain available:

```bash
curl -sS http://127.0.0.1:8080/healthz
curl -sS http://127.0.0.1:8080/.well-known/agent-card.json
```

Every standalone server keeps FastAPI's generated `/openapi.json`, `/docs`, and
`/redoc` endpoints. In managed mode that schema contains only the stable Harnest
HTTP API. For advanced-mode ADK applications only, the launcher also mounts
ADK's official routes as a native surface:
ADK session paths under `/apps/{app}/users/{user}/sessions`, JSON Event arrays
from `/run`, Event streams from `/run_sse`, and `/run_live`. Those routes expose
ADK-native types and can vary with the installed ADK version; new integrations
should use the neutral routes above. Inspect `/docs` or `/openapi.json` for the
exact surface compiled for the selected mode.

This standalone path needs the artifact's Python dependencies and any model or
MCP services used by the agent. A compiled folder is not a bundled Python
environment, and the local server does not apply `config.yaml` resource limits,
secret resolution, permissions, scaling, authentication, or TLS. Set runtime
environment variables explicitly. `python .harnest/helpdesk` starts the same server with default
host and port. Non-loopback binds are rejected unless the launcher receives
`--allow-remote` (for Make, set `SERVE_EXTRA_ARGS=--allow-remote`). Binding
beyond loopback exposes an unauthenticated endpoint; add a trusted reverse proxy
and transport security before doing so.

## Logging and tracing

Authored code gets a framework-neutral structured logger and tracer from the
compiler-provided namespace. Bound attributes are copied into JSON console logs
and, when OTLP is configured, into OpenTelemetry log records. Logs created inside
a span carry the same trace and span identifiers:

```python
from harnest.logging import get_logger
from harnest.tracing import span, traced


logger = get_logger("catalog").bind(component="search")


@traced("catalog.lookup", attributes={"catalog.kind": "products"})
def lookup(query: str):
    with span("catalog.rank", candidate_count=12):
        logger.info("catalog.lookup.completed", result_count=3)
        return ["one", "two", "three"]
```

Harnest emits one `harnest.agent.invoke` span for direct, HTTP, SSE, and each
WebSocket response run, and instruments the FastAPI server. It records
low-cardinality runtime attributes, never prompt/response bodies or raw session
IDs. The scaffold also disables ADK/GenAI message-content capture by default;
deployments can explicitly override those environment values.

Set a standard OTLP/HTTP endpoint to export traces and logs to an OpenTelemetry
Collector or compatible backend:

```bash
export OTEL_SERVICE_NAME=support-agent
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export HARNEST_LOG_LEVEL=INFO
harnest serve .harnest/support-agent
```

`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` and
`OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` enable individual signals. Harnest also
honors the standard per-signal headers, timeouts, compression, resource, and
sampling variables accepted by the OpenTelemetry Python SDK. Put
`OTEL_EXPORTER_OTLP_HEADERS` in `spec.secrets`, not plain `spec.environment`.
`HARNEST_LOG_CONSOLE=false` disables JSON stderr logs;
`HARNEST_OTEL_EXCLUDED_URLS` overrides the FastAPI exclusion list, otherwise
`OTEL_PYTHON_FASTAPI_EXCLUDED_URLS` is respected. The bundled exporter is
`http/protobuf`; use an OpenTelemetry Collector for fan-out or protocol
translation.

## Development

```bash
python -m pip install -e ".[all,quality]"
make quality
```

The quality gate runs both test suites and schema checks, enforces cyclomatic
complexity of at most 10 in Python and Go, verifies Go formatting, and runs
`go vet` plus the offline Python-to-Go integration example. See
[Harnest development standards](docs/development.md) for the design,
database-access, OTEL auditing, comments, and test requirements.

Use `make test` when only the suites and schema syntax checks are needed, and
`make validate-examples` to run the offline helpdesk author tests, render the
Python plan, and dry-run every enabled example agent. The package requires
Python 3.10 or newer; if `python3` resolves
to an older system interpreter, the Makefile selects an available versioned
binary from Python 3.10–3.14. Override it when needed, for example
`make test PYTHON=python3.12`.

Run the helpdesk's official ADK eval against the configured Ollama model with
`make example-eval`, or run unit, smoke, and eval lanes together with
`make example-all`. Its in-order trajectory criterion permits skill-loading
calls while still requiring the expected `triage_request` call.
