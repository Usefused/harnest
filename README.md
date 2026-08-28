# Harnest

Harnest is a filesystem-first compiler and standalone runtime for self-serve
agents. Its managed authoring layer supports Google ADK and LangGraph, lowers a
small graph API to the selected framework, and serves both through the same
HTTP/WebSocket contract. Agent authors put instructions, tools, plugins,
extensions, subagents, MCP connections, skills, evals, and tests in conventional
folders; ordinary reusable Python lives under `lib/`. The native CLI validates
that source, compiles it, and runs the result.

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
managed runtime installs the compiler's core only; the selected framework,
provider-specific, and agent-specific packages live in each agent environment. Pin
or redirect the source with `HARNEST_VERSION` and
`HARNEST_REPO=owner/repository`. See [Installation and
releases](docs/releases.md) for verification, private-repository, upgrade, and
maintainer instructions.

Create and exercise an agent with compiler-owned authoring imports:

```bash
harnest skills install
harnest init support-agent
harnest env sync support-agent
harnest test support-agent
harnest compile support-agent --output .harnest/support-agent
harnest serve support-agent
```

`init` is intentionally quiet: it creates one runnable agent and ignored
`_README.md` guides in optional resource and test folders. Those guides are not
compiled. Use `harnest init support-agent --example` when you want the complete
working graph, tool, plugin, skill, extension, eval, and test examples.

Each agent has an isolated, compiler-managed environment below
`AGENT_DIR/.harnest/environments/`. `harnest env sync AGENT_DIR` resolves its
`pyproject.toml`, writes or refreshes `uv.lock`, installs the release's embedded
Harnest wheel with only the selected framework extra, and caches the resulting
interpreter by dependency fingerprint. Users do not activate this environment.
`compile`, `test`, and `serve` synchronize it automatically; CI can run
`harnest env sync AGENT_DIR --frozen` to require the committed lock without
changing it. Add provider, tool, and library packages to `[project].dependencies`.
Do not add ADK, LangGraph, or their Harnest-owned adapters: the selected
framework is installed from the matching Harnest release and direct declarations
are rejected before dependency resolution.

Upgrade an agent created with an older Harnest filesystem contract in two
explicit steps:

```bash
harnest upgrade existing-agent
harnest upgrade existing-agent --apply
```

The first command is read-only and lists every create, rewrite, and move plus
anything that requires manual resolution. `--apply` refuses blocked plans,
prints its fresh effective plan before mutation, verifies its source hashes,
and backs up every affected file
under `existing-agent/.harnest/upgrade-backups/`, and only then migrates it. It
preserves authored business logic while updating recognized structural
contracts such as `requirements.txt` to `pyproject.toml`, `mcp_servers/`, MCP
exports, and legacy extension wiring.
After applying, review the diff and run `harnest test existing-agent`.

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
schemas/                             Config, card, server, and plan JSON Schemas
examples/self-serve/
└── agents/
    └── helpdesk/
        ├── config.yaml              Compute, scaling, env, secrets, permissions
        ├── harnest.lock             Committed project-schema migration marker
        ├── server.yaml              Standalone HTTP limits and playground policy
        ├── agent-card.yaml          A2A 1.0 discovery metadata
        ├── agent.py                 Root definition using harnest.* imports
        ├── instructions.md           Required root instructions
        ├── pyproject.toml            Agent dependency declarations
        ├── uv.lock                   Resolved dependency lock after sync
        ├── lib/                      Reusable Python under harnest.lib
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
`google-adk` and `langgraph` constraints published in Harnest's own package.
Generated agent `pyproject.toml` files contain only agent-owned dependencies.
The compiler rejects selected-framework declarations there and checks the
installed distribution before loading authored agent code.

This contract applies equally to managed and advanced mode. Advanced authors
still import ADK or LangGraph directly, but direct imports are an authoring
escape hatch rather than a way to bypass Harnest's tested runtime boundary. To
adopt a newer framework release, upgrade Harnest to a release that declares
support for it and run the agent tests. Agent metadata cannot independently
override that framework contract.

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
message=..., state_delta=...)`; `message` emits assistant text. To build
stateful orchestration without a framework plugin, declare a `context`
parameter, read its immutable `GraphContext.state`, and return only intended
session changes through `state_delta`. `START` marks entry edges and a
`Join` waits for all incoming branches. Graph construction rejects unknown edge
references, duplicate edges, and unreachable nodes before either backend runs.

The one-agent managed form stays intentionally small:

```python
import os

from harnest.agent import Agent
from harnest.model import LiteLLMModel


root_agent = Agent(
    name="support",
    history="session",
    model=LiteLLMModel(
        model=os.getenv("LITELLM_MODEL", "ollama_chat/qwen3.5:cloud"),
        api_base=os.getenv("LITELLM_API_BASE", "http://127.0.0.1:11434"),
        thinking=True,
    ),
    description="Answers product questions and triages support requests.",
)
```

For ADK this builds an `LlmAgent`; for LangGraph it builds a LangChain tool-loop
graph. `history="session"` is the default and exposes prior user/assistant turns
from the same Harnest session. Set `history="turn"` for an intentionally
isolated model call. This contract also applies to `Agent` nodes inside a
portable `Graph`; the Harnest runtime remains the single session authority.
`LiteLLMModel` and `OllamaModel` have
adapters for both frameworks. Provider-specific Python dependencies still
belong in the agent's `pyproject.toml`.

Both connectors support thinking and non-thinking models. Set `thinking=True`
to request reasoning, `thinking=False` to disable it, or omit the option to use
the provider default. For an exact provider-supported level, pass
`reasoning_effort="low"`, `"medium"`, or `"high"` instead of `thinking`.
Harnest preserves framework/model reasoning state for subsequent turns but
never places hidden thought parts in `/responses`, SSE, `/live`, or eval output.
If a provider completes with reasoning only and no visible answer or structured
result, JSON returns `502` and streaming transports emit an `error` event rather
than reporting a successful empty response.

Teams that route models through a gateway can give `LiteLLMModel` a
`LiteLLMLifecycle` instead of patching LiteLLM globally. Its per-model hooks
create a provider client once, transform each request and response, observe
errors, and release the client at agent shutdown:

```python
import os

import httpx
from openai import AsyncOpenAI

from harnest.model import LiteLLMLifecycle, LiteLLMModel


class TeamGateway(LiteLLMLifecycle):
    async def create_transport(self, context):
        self.http = httpx.AsyncClient(
            cert=("/run/secrets/model.crt", "/run/secrets/model.key")
        )
        return AsyncOpenAI(
            base_url="https://models.internal/v1",
            api_key=os.environ["MODEL_GATEWAY_TOKEN"],
            http_client=self.http,
        )

    async def before_request(self, request, context):
        request.setdefault("extra_headers", {})["X-Team-ID"] = "support"
        return request

    async def close(self, context):
        await self.http.aclose()


model = LiteLLMModel("openai/support", lifecycle=TeamGateway())
```

The object returned by `create_transport` is passed through LiteLLM's supported
per-call `client` option; return a provider SDK client such as `AsyncOpenAI`,
not a bare HTTP client. Hooks may be synchronous or asynchronous. Async hooks
require async model execution, and one built model cannot mix sync and async
calls. For streamed calls, `after_response` runs as an observer only after the
stream is exhausted successfully, while iteration failures reach `on_error`.
Shutdown waits for in-flight calls and retries remain possible after failed
cleanup. Lifecycle instances and transports are never shared implicitly.

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

Advanced mode does not perform managed capability composition: authored source
owns native tools, MCP clients, subagents, prompts, routing, state, checkpoints,
middleware/plugins, framework upgrades, and arbitrary native model calls.
Harnest still owns the neutral server, decorated authentication, and portable
invocation extensions. Explicit Harnest-decorated tools keep the same approval,
identity, session, tracing, and suspension context when reached through neutral
JSON, SSE, or WebSocket execution; advanced source must identify those native
capabilities because opaque targets are not automatically inspected. Portable model interception is guaranteed only
through Harnest-managed or wrapped model boundaries; otherwise use an explicit
native lifecycle factory.

`Agent.advanced(...)` is also composable. A managed ADK root may include it in
`subagents`, and a portable ADK or LangGraph `Graph` may use it as a native node:

```python
native_researcher = Agent.advanced(
    target=native_agent,  # native_agent.name == "researcher"
)

root_agent = Agent(
    name="support",
    model=model,
    subagents=[native_researcher],
)
```

Embedded targets cannot use root `input_adapter` or `output_adapter` functions;
their containing agent or graph already owns that boundary. Embedded ADK
subagents accept `BaseAgent`, not `App`, and an optional wrapper name must match
the native agent name because ADK uses that identity for delegation. Export
advanced filesystem subagents as flat `subagents/<name>.py` resources; nested
subagent folders are reserved for Harnest-composed managed agents.

To assess an existing edited project without rewriting any source, run:

```bash
harnest mode advanced support-agent --check
```

The check is read-only: it reports the current framework, mode, entrypoint, and
managed folders that need explicit framework wiring and clearly separates what
Harnest still owns from native responsibilities. It never regenerates
`agent.py`, moves resources, changes `config.yaml`, or discards user
modifications. Use
`harnest init support-agent --mode advanced` only when creating a new project;
migrate existing code deliberately after reviewing the report.

These imports are supplied by the compiler/runtime and do not make Harnest an
agent-owned dependency: do not add `harnest` to `pyproject.toml`. Authored
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
`order_specialist`; and `mcp/catalog.py` must export a zero-argument `client()`
factory returning `MCPClient`. The filename supplies the identity `catalog`.
An `@client_tool` export follows the same tool filename/signature contract, but
its Python body is only a declaration: the connected client executes it.

### Reusable Python library

Put ordinary shared implementation in root `lib/`. Harnest mounts
`lib/audit.py` as `harnest.lib.audit` and nested modules such as
`lib/storage/queries.py` as `harnest.lib.storage.queries`:

```python
from harnest.lib.storage.queries import load_order
from harnest.tool import tool


@tool
def lookup_order(order_id: str) -> dict:
    """Load one order."""
    return load_order(order_id)
```

The library is root-only and global to the compiled bundle in managed and
advanced mode. It is ordinary importable code, not a discovered capability:
functions in `lib/` never become tools or agents unless an authored resource
explicitly uses them. No `__init__.py` is needed; add one only for intentional
library initialization. Do not create a nested agent `lib/` or import it as bare
`lib.*`. The `harnest.lib.*` namespace works consistently in
compilation, unit and smoke tests, evals, and the standalone server. Keep
third-party library dependencies in `pyproject.toml`.

### Execution context resources

Publish reusable runtime values explicitly with `@context("name")`. Used by
itself, the provider runs once per invocation. Combine it with
`@lifecycle.resource` when Harnest should start the provider once for the
application and close it during shutdown:

```python
# extensions/memory.py
from harnest.context import context
from harnest.lifecycle import lifecycle
from harnest.lib.memory.bigquery import BigQueryMemory


@lifecycle.resource
@context("memory")
async def memory():
    client = BigQueryMemory()
    try:
        yield client
    finally:
        await client.close()
```

`@lifecycle.resource` owns startup and shutdown but keeps the value private;
`@context("memory")` publishes the returned or yielded value. Nodes, tools, and
subagents running inside Harnest retrieve the same value with
`context.resource("memory")`. Context providers may also publish storage or a
checkpointer when direct access is intentional. Duplicate names and provider
conflicts fail compilation; unknown names and access outside an active
invocation fail clearly at runtime. Harnest binds context for `/responses`,
`/live`, `run_agent_message`, and managed nodes, tools, and subagents. Direct native
framework endpoints and native targets invoked outside Harnest do not receive
it. See [Runtime extensions](docs/extensions.md).

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

Root `extensions/**/*.py` files may use `@lifecycle.*` decorators on any number
of functions. Multiple files can share a phase and run by explicit order, then
source path. Portable hooks cover authentication, invocation, events, errors,
and managed model calls; explicit `@lifecycle.adk_plugin` and
`@lifecycle.langgraph_middleware` factories retain native framework control.
See the [runtime extension contract](docs/extensions.md).

An optional `sandbox/sandbox.py` exports one `Sandbox`. Built-in container
sandboxes deny network access by default; provider packages can supply another
ADK executor. See the [sandbox contract](docs/sandbox.md) for the exact security
boundary and dependency requirements. Managed LangGraph compilation rejects
unsupported sandbox configuration instead of silently dropping it.

Every optional resource directory may be absent or empty. Empty directories are
ignored during compilation; once a public resource is present, its filename and
export contract are validated strictly. Default `harnest init` creates ignored
guides rather than active capabilities; `--example` opts into working starter
resources that demonstrate each convention.

Authored files are compiler input. Their `harnest.*` imports resolve while the
compiler or compiled runtime is active; the agent's provider-specific
dependencies remain in its `pyproject.toml`. Use `harnest compile` or `harnest
serve`; both synchronize and use the isolated agent environment. Import-free authored
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

The default `business` trajectory requires authored business tool calls in
order but permits extra discovery and progressive `load_skill` calls. Use the
separate strict lane when every tool call must match exactly:

```bash
harnest test examples/self-serve/agents/helpdesk --evals \
  --eval-trajectory strict
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
import os

from harnest.mcp import MCPClient


def client():
    return MCPClient.streamable_http(
        os.environ["CATALOG_MCP_URL"],
        headers={"Authorization": f"Bearer {os.environ['CATALOG_MCP_TOKEN']}"},
    )
```

Use `MCPClient.sse(...)` inside the same factory for a legacy SSE server.
`timeout_seconds` bounds connection and MCP operation work;
`sse_read_timeout_seconds` independently controls how long an idle SSE stream
may remain quiet. The default is five minutes for both ADK and LangGraph. The
`client()` signature must literally have no parameters; use `os.environ` or
other ordinary Python inside the body. Harnest redacts factory exception
messages, assigns each discovered client a stable path-scoped capability ID,
and rejects duplicate connection configurations even when approval metadata or
local identities differ.

Managed LangGraph supplies approval policies through
`MultiServerMCPClient`'s native tool interceptor. The gate runs immediately
before MCP network execution and routes on the server plus original unprefixed
tool name, so it does not intercept ordinary local tools or leak policy between
servers.

## Folder contract

Every deployable directory must contain:

- `config.yaml`: deployment and compiler selection, including
  `spec.framework.name` (`adk` or `langgraph`), optional mode (`managed` by
  default or `advanced`), resources, scaling,
  environment, secret references, and permissions. Secret values are never
  allowed; `secretRef` points at the engine's secret provider.
- `server.yaml`: standalone compiled-server binding, request limits, and
  playground policy. It does not contain deployment scaling, authentication,
  session storage, TLS, or secrets.
- required storage lifecycle: exactly one root `@lifecycle.session_store` and
  one root `@lifecycle.checkpointer` factory. The default
  `extensions/storage.py` returns one shared store from `lib/storage.py`;
  teams may split the listeners across files. Compilation rejects missing,
  duplicate, hidden, or competing authorities.
- `agent-card.yaml`: the public A2A 1.0-facing description, interfaces,
  modalities, capabilities, and skills supported by the current runtime.
- the Python source module named by `spec.entrypoint` using `module:symbol`
  syntax. Managed mode exports an `Agent` or `Graph`; advanced mode exports an
  `Agent` created with `Agent.advanced(...)`. The compiler turns it into the
  runtime module.
- optional sibling `tools/`, `subagents/`, `mcp/`, and `sandbox/` directories
  whose public files follow the filename-matched export conventions above;
  root-only `plugins/<name>/{mcp,skills}` capability bundles; and root-only
  `extensions/**/*.py` decorated runtime lifecycle modules. Folder-based nested
  subagents get their own supported sibling resource scope as described above.
- optional root-only `lib/` containing ordinary reusable Python imported below
  `harnest.lib`; it is global to the bundle and is not resource discovery.
- non-empty UTF-8 `instructions.md` (also required as bundle metadata in advanced
  mode), plus optional `skills/`, ADK-only `evals/`, and
  test-only `tests/unit/` and `tests/smoke/` directories following the
  conventions above.
- the required `pyproject.toml` and resolved `uv.lock` when present.

Unknown YAML fields fail validation. Compilation is deterministic and ignores
virtual environments and caches when hashing or copying source.

Agent-owned Python files use explicit `harnest.*` authoring imports alongside
standard-library and third-party imports. The compiler/runtime supplies the
Harnest namespace, so it is not listed in the agent's `pyproject.toml`.

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

It reads the generated artifact's adjacent `server.yaml`; no server flags are
required. Invoke the generated launcher directly the same way:

```bash
.harnest/helpdesk/harnest-agent
```

The authored file is validated during compilation and copied beside the
launcher. Its `http` section controls host, port, remote-binding permission,
response timeout, and concurrency. `limits.maxRequestBytes` accepts bytes or a
binary unit such as `10MiB` (1 KiB through 1 GiB) and applies to every HTTP body
and WebSocket frame, including advanced ADK-native routes.
`playground.enabled` controls only Harnest's `/` and `/_harnest/` development
UI; OpenAPI, docs, and agent APIs remain available when it is disabled.

The adjacent compiled copy is deliberately mutable deployment policy, so it is
the only runtime file outside the artifact digest. The authored copy remains
hashed under `source/server.yaml`. Replace the adjacent copy to configure an
already compiled artifact, then restart it; malformed, symlinked, or missing
configuration fails closed. Explicit `harnest serve` or launcher flags remain
temporary operator overrides, while the file is the durable source of truth.

Any setting scalar may instead be an exact `${NAME}` environment reference.
The launcher resolves it at startup and then applies the field's normal string,
boolean, integer, number, or binary-size validation. Missing, empty, or invalid
values fail startup while naming only the variable and field. `$NAME`, partial
interpolation such as `server-${HOST}`, and environment references in
`apiVersion` or `kind` are rejected. Compilation preserves references verbatim;
resolved values never enter the source copy, manifest, or diagnostics.

```yaml
apiVersion: harnest.dev/v1alpha1
kind: Server
http:
  host: 127.0.0.1
  port: ${AGENT_PORT}
  allowRemote: false
  requestTimeoutSeconds: 300
  maxConcurrentRequests: 8
limits:
  maxRequestBytes: ${AGENT_MAX_REQUEST_BYTES}
playground:
  enabled: true
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

Tools and MCP calls decorated with `require_human_approval` stop before
execution. JSON returns `status: "requires_action"`; SSE and `/live` emit
`approval.requested` followed by a completed `requires_action` response. Submit
`{"decision":"approve"}` or `{"decision":"deny"}` to
`POST /approvals/{approvalId}`. Approval is principal-, session-, invocation-,
action-, and argument-bound, expires closed, and is consumed once. Approval
continues the exact suspended task: the invocation is not rerun, earlier side
effects are not replayed, and a later protected call can request another
approval. The standalone store is process-local, so pending approvals and
suspended tasks do not survive restart. The same workflow protects explicitly
decorated capabilities in advanced roots and advanced subagents while they run
through Harnest's neutral execution boundary. Calls made through direct native
framework routes, detached background tasks, or opaque capabilities that were
not decorated remain framework-owned; a decorated call outside the Harnest
boundary fails closed instead of executing unprotected.
The bundled playground renders the same request with Approve and Deny controls.
Its Trace inspector keeps a bounded, process-local history of request stages,
tool activity, failures, and structured `harnest.agent.*` logs for the current
authenticated principal. This development view complements the durable OTLP
export below; disabling the playground also disables its trace buffer and
private `/_harnest/traces` routes.

Client-hosted tools use the same resumable boundary without pretending Harnest
contains a browser or desktop kernel. Declare a typed stub with
`from harnest.tool import client_tool`. JSON and SSE return a
`requiredAction` of type `client_tool` containing its request ID, name, and
arguments; submit `{"output": ...}` to `POST /client-tools/{requestId}` to
resume the exact task. On `/live`, answer `client_tool.requested` on the same
socket with
`{"type":"client_tool.result","requestId":"client_tool_...","output":...}`.
Results are principal-bound and one-time. The caller owns execution policy,
sandboxing, and result validation appropriate to its browser, desktop, or
mobile environment.

Tool calls and results appear in `output` as ordered `tool_call` and
`tool_result` items. Reuse the session ID for conversational continuity. Session
CRUD is `GET /sessions`, `GET /sessions/{id}`, `PATCH /sessions/{id}` with the
exact body `{"stateDelta": {...}}`, and `DELETE /sessions/{id}` (204). Sessions
are process-local by default and disappear when the launcher stops.

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
may emit `client_tool.requested` as described above. It also accepts
`{"type":"session.close"}`. There is no mode flag or HTTP-to-WebSocket
rerouting.

The ADK and LangGraph runtime adapters map provider events into the same neutral
message, tool-call, and tool-result items. Provider names, model versions,
reasoning details, and framework bookkeeping are not exposed. Before a stream
starts, request errors use FastAPI's JSON `{"detail":"..."}` shape with normal
HTTP status codes; SSE and WebSocket execution errors use a typed `error` event.
The executable server has no configured identity provider by default. A root
`@lifecycle.authenticate` pipeline or embedding host can resolve the existing
`AuthPrincipal` independently from session storage. It receives a stable,
read-only Harnest connection context rather than a FastAPI object;
that principal's `user_id` scopes every neutral session, response, SSE stream,
and WebSocket. The middleware also requires authentication for advanced ADK
native routes, but their native user fields still require deployment
authorization. Health, Agent Card, and `/agent` discovery remain public. A
missing authenticator preserves the local anonymous behavior.

Session and checkpoint persistence are required root lifecycle resources.
`harnest init` creates one development `MemoryStore` in `lib/storage.py` and
returns it from both factories in `extensions/storage.py`. Replace it with a
durable Harnest store or custom implementation before production. Harnest
starts and closes a shared object only once. The session side scopes ADK and
LangGraph sessions across JSON, SSE, and WebSocket transports:

```python
from harnest.lifecycle import lifecycle
from harnest.lib.storage import store
from harnest.runtime_auth import AuthPrincipal, AuthenticationError


@lifecycle.session_store
def session_store():
    return store


@lifecycle.checkpointer
def checkpointer():
    return store


@lifecycle.authenticate
async def authenticate(connection, _principal):
    token = connection.headers.get("authorization")
    if token != "Bearer deployment-validated-token":
        raise AuthenticationError()
    return AuthPrincipal("tenant-user-id")
```

The two storage factories may share a file; the authenticator may be another
lifecycle extension or a host injection. Duplicate factories fail compilation,
and lifecycle session storage cannot be combined with host-injected session
storage. Use `PostgresStore` for the recommended production path, or
`RedisStore` when its persistence and expiry trade-offs fit the deployment.
Managed mode creates native ADK/LangGraph adapters; advanced mode still
declares ownership through the lifecycle. See
[checkpoint ownership and production stores](docs/checkpoints.md).

Production authenticators should validate real bearer/OIDC credentials and
derive stable, non-secret user IDs; the example only shows the injection shape.

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

Open `http://127.0.0.1:8080/` for Harnest's bundled development playground. It
uses only `/agent`, `/sessions`, `/responses`, and `/live`, so the same UI tests
managed or advanced ADK and LangGraph agents. The playground can create and
select sessions, inspect state, show tool calls/results, and run complete JSON,
SSE streaming, or WebSocket live conversations. No frontend build or ADK web UI
is required.

An optional bearer token entered in the playground is held only in page memory
and sent on HTTP/SSE requests. It is never placed in storage or a URL. Browsers
cannot add arbitrary authorization headers to WebSocket handshakes, so protected
`/live` deployments must use a same-origin authentication cookie.

This standalone path needs the artifact's Python dependencies and any model or
MCP services used by the agent. A compiled folder is not a bundled Python
environment, and `server.yaml` does not apply `config.yaml` deployment
resources, resolve secrets, enforce permissions, scale replicas, inject
authentication, provision an external session database, or terminate TLS. Set
runtime environment variables explicitly. `python .harnest/helpdesk` reads the same adjacent
configuration. A non-loopback `http.host` requires `http.allowRemote: true`.
That permission does not add authentication; inject an authenticator or place a
trusted TLS reverse proxy in front before exposing the process.

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
