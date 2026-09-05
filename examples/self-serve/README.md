# Self-serve example

This project demonstrates the authoring boundary: agent owners write Python and
YAML, while the platform runs one shared Go discovery and deployment engine.

To add another agent, copy `agents/helpdesk` to a new immediate child directory,
then change at least:

1. `config.yaml` → `metadata.name`, `spec.entrypoint`, runtime requirements,
   resources, secret references, and permissions;
2. `agent-card.yaml` → public identity, deployed interface URL, modalities, and
   skills; and
3. `agent.py` → the root `Agent` definition using explicit `harnest.*` imports;
4. `instructions.md` → the required non-empty root instruction;
5. `models/**/*.py` → shared Pydantic contracts imported through `harnest.models`;
6. `tools/<name>.py` → an `@tool` callable exported as `<name>`;
7. `subagents/<name>.py` → an `AgentDefinition` exported as `<name>`;
8. `mcp/<name>.py` → an `MCPClient` or `None` exported as `<name>`;
9. `lifecycle/storage.py` and `lib/storage.py` → shared session and checkpoint ownership;
10. `skills/<name>/SKILL.md` → optional progressive Agent Skills; and
11. `evals/<id>.evalset.json` → optional official ADK eval sets; and
12. `tests/unit/test_*.py` and `tests/smoke/test_*.py` → offline and opt-in
    live-model tests.

The existing `orchestrator.py` selects every child under `agents/`, so no Go
registration is needed. Use `include` or `exclude` when only part of a source
root should be deployed, or add another `AgentSource.directory(...)` for a
different root.

From the repository root:

```bash
make plan
make dry-run
```

`plan` shows the versioned JSON consumed by Go. `dry-run` exercises the source
tree's existing development environment. The installed CLI instead runs
`harnest env sync examples/self-serve/agents/helpdesk`, creates the isolated
agent environment, and locks declared dependencies before compile, test, or
serve. URLs and secret references remain deployment placeholders.

## Live local run

The helpdesk agent and its technical subagent use
`LiteLLMModel.from_openai_environment()`. They share `OPENAI_MODEL`,
`OPENAI_BASE_URL`, and `OPENAI_API_KEY` with evaluation judges and simulators.
The checked-in non-secret configuration selects `gpt-4.1-mini` at
`https://api.openai.com/v1`. Export `OPENAI_API_KEY` from your shell or CI secret
store; Harnest does not load `.env` files. The `spec.secrets` entry is an
illustrative deployment mapping and is not resolved by local commands.

From the repository root:

```bash
export OPENAI_API_KEY="..."
make example-install
make live-run
```

This installs Harnest from the working tree, including its ADK and LiteLLM
runtime dependencies, compiles the flat source folder into
`.harnest/helpdesk`, and starts the ADK interactive CLI against that generated
artifact. The Make target exports model and endpoint settings for direct
launcher use. For an OpenAI-compatible Ollama endpoint, use
`OPENAI_MODEL=openai/qwen3.5:9b` and
`OPENAI_BASE_URL=http://127.0.0.1:11434/v1`, with that model installed locally.
Use `OPENAI_API_KEY` for any endpoint credential; do not introduce an
`OLLAMA_API_KEY`. Update the same non-secret values in `config.yaml` for
`harnest test`, `run`, and `serve`, because `spec.environment` overrides matching
shell values. See the canonical
[model credential process](https://docs.usefused.com/harnest/build/project-configuration#configure-model-credentials).

Run `make example-test` for the offline unit suite. It uses the
injected `agent` and read-only `tools` fixtures to check filesystem composition
and call `triage_request` directly; it does not invoke a model.

Run `make example-smoke` (or the retained `make live-test` alias) for the
explicit live acceptance check. The `smoke` fixture sends only benign synthetic
demo data through the neutral `/responses` API and verifies the model calls
`triage_request` and returns the expected `technical-support / urgent` result.
The `--smoke` command always runs the unit suite first.

Run `make example-eval` to execute the offline unit suite and then every
validated checked-in ADK eval set. Harnest automatically applies
`evals/test_config.json`; the example's in-order tool-trajectory criterion
allows extra skill-loading calls but requires the expected `triage_request`
call. Like `live-run`, evals require the configured model endpoint and its
credentials. `make example-all` selects unit tests, live smoke tests, and evals in one
command; Python tests must pass before eval execution begins.

The remote knowledge MCP connection is optional. Without
`KNOWLEDGE_MCP_TOKEN`, the example uses only its local triage tool and subagent,
which avoids an additional MCP credential requirement.

The test modules themselves import no Harnest package. `agent` exposes the
compiled ADK root, `tools` maps local tool names to callables, and smoke-only
tests may request either the high-level `smoke` helper or a raw FastAPI `client`.
Unit tests are offline by convention—the runner withholds HTTP fixtures but does
not install an operating-system network sandbox. Neither test directory is
composed into agent instructions, resources, or advertised capabilities.
All smoke tests share one server and `MemoryStore` lifecycle. Do not close the
fixtures yourself. Omit `session_id` for an isolated request, or use a unique
explicit ID within one test when verifying multi-turn behavior.

## Standalone HTTP server

The compiled artifact runs without the Go provisioner. After running
`make example-install`, start its generated launcher from the repository root:

```bash
make serve-example
```

`serve-example` compiles first and reads the authored server overrides from the
root `server:` section of the example's `config.yaml`. In
another terminal, inspect the agent, create a session, and run an agent turn
through Harnest's neutral API:

```bash
curl -sS http://127.0.0.1:8080/agent

curl -sS -X POST http://127.0.0.1:8080/sessions \
  -H 'Content-Type: application/json' \
  --data '{"id":"demo-session","state":{}}'

curl -sS -X POST http://127.0.0.1:8080/responses \
  -H 'Content-Type: application/json' \
  --data '{"input":"Triage a fictional production API authentication outage.","sessionId":"demo-session"}'
```

The final call returns `id`, `status`, `sessionId`, `outputText`, ordered
provider-neutral `output` items, and `metadata`. Reuse the session for a
follow-up, or set `stream: true` for named SSE events:

```bash
curl -N -sS -X POST http://127.0.0.1:8080/responses \
  -H 'Content-Type: application/json' \
  --data '{"input":"What should I collect next?","sessionId":"demo-session","stream":true}'
```

The stream begins with `response.created`, emits text deltas and any tool
call/result events, then ends with `response.completed`. Each event includes a
sequence number plus response and session IDs. `WS /live` uses the same event
vocabulary: first send `{"type":"connect","sessionId":"demo-session"}`, wait
for `session.connected`, then send a `response.create` frame with `input` and
optional `requestId`/`metadata`. Send `session.close` to finish; `/live` does not
use a mode flag.

Session CRUD is available at `/sessions` and `/sessions/{id}`; PATCH accepts
exactly `{"stateDelta": {...}}`. Sessions are in memory and last only for this
process. HTTP request failures use `{"detail":"..."}`; failures after an SSE or
WebSocket stream starts use a typed `error` event. The matching helpers are
`make demo-agent`, `make demo-session`, `make demo-response`, and
`make demo-stream`.

Health and Agent Card discovery are also available:

```bash
curl -sS http://127.0.0.1:8080/healthz
curl -sS http://127.0.0.1:8080/.well-known/agent-card.json
```

This managed example exposes only Harnest's neutral routes. Advanced-mode ADK
artifacts additionally mount official ADK `/run`, `/run_sse`, `/run_live`, and
`/apps/{app}/users/{user}/sessions` routes. Inspect `/docs` or `/openapi.json`
for the exact compiled surface.

The artifact runs through the synchronized agent interpreter selected by
`harnest serve`; it still needs a reachable model and any enabled MCP services.
The standalone server does not
read deployment environment from `config.yaml` or provide the provisioner's
secret resolution, resource enforcement, permissions, scaling, authentication,
or TLS. The Make target exports the model settings; export any optional MCP
variables yourself. The authored Agent Card is served unchanged, including its
deployment URL. For direct use, run `.harnest/helpdesk/harnest-agent serve`; it reads
the adjacent compiled `server.yaml` without flags. That file configures binding,
timeout, concurrency, request size, and the playground. It does not inject auth,
storage, or TLS. A non-loopback host requires `http.allowRemote: true`; still add
authentication and TLS through a trusted proxy before exposing it.

## Filesystem composition contract

Agent-owned source imports its compiler-provided types explicitly. The root uses
`from harnest.agent import Agent`; tool files use
`from harnest.tool import tool`; model connectors use
`from harnest.model import LiteLLMModel`; and MCP files use
`from harnest.mcp import MCPClient`. Harnest belongs to the compiler/runtime, so
it is not listed in the agent's `pyproject.toml`. The compiler examines
sibling resource directories, so root code still never maintains an import or
registration list.

Managed `Agent` definitions default to `history="session"`, including graph
nodes, so reusing a `/responses` or `/live` session provides portable multi-turn
context on ADK and LangGraph. Use `history="turn"` only for intentional model
isolation; do not maintain a second transcript in graph state.

`harnest compile` is the source entrypoint and supplies the namespace while
loading authored code; ADK receives only the generated package. Authored files
must import every Harnest symbol explicitly; bare magic globals are rejected. The
import-free orchestrator similarly runs through `harnest plan`.

Portable graph construction remains managed mode. If this example eventually
needs direct ADK or LangGraph features that Harnest's graph cannot express,
audit it before migrating:

```bash
harnest mode advanced examples/self-serve/agents/helpdesk --check
```

The check is read-only and preserves every change already made to the example.
It reports managed resources that would need explicit wiring; it does not alter
`config.yaml`, regenerate `agent.py`, or move folders. An advanced entrypoint
imports its framework directly and exports
`Agent.advanced(name=..., target=...)`; Harnest does not provide a wrapped ADK
or LangGraph package.

- `instructions.md` is required, non-empty UTF-8. The compiler supplies it when
  `Agent.instruction` is omitted; an explicit nonblank instruction wins without
  merging, but the file is still validated.
- `tools/<name>.py` must export a callable named `<name>` decorated with
  `@tool`. Use one public resource file per discovered tool.
- `subagents/<name>.py` must export exactly one `AgentDefinition` named
  `<name>`.
- `mcp/<name>.py` must export an `MCPClient` or `None` named `<name>`.
  Exporting `None` is the supported way to disable an optional integration.
- `plugins/<folder>/plugin.json` declares an Agent Plugins 1.0 package.
  Optional `mcp.json` supplies standard MCP servers; optional `skills/` supplies
  progressive instructions. Neither component requires the other. See the
  [Agent Plugins guide](https://docs.usefused.com/harnest/build/agent-plugins).
- `lifecycle/*.py` declares lifecycle hooks and resource factories.
- `extensions/<name>/extension.yaml` declares a Harnest Extension whose
  `extension.py` exports the singleton `extension`.
  Optional `adk.py` or `langgraph.py` files provide native integration for the
  selected framework.
- `__init__.py`, dotfiles, caches, and underscore-prefixed helper files are
  ignored.

Any optional resource root can be missing, empty, or contain only ignored
starter/helper files. Compilation skips it and continues. Public files and
directories are validated strictly once present. `harnest init` gives every
resource folder starter content, including an inline graph agent, an
environment-gated MCP-and-skill plugin, lifecycle hooks, and an ignored
sandbox example that can be renamed to `sandbox.py` when isolation is
configured.

Each public directory directly under `skills/` is one Agent Skill. Its name
must be kebab-case and its `SKILL.md` frontmatter `name` must match the directory:

```text
skills/
└── incident-triage/
    ├── SKILL.md
    ├── references/   # optional
    ├── assets/       # optional
    └── scripts/      # optional
```

ADK and LangGraph receive the same progressive list/load/resource tools instead
of injecting every skill body into the prompt. Symlinks are rejected.

These internal skills are distinct from the public capabilities advertised by
the Agent Card's `skills` field. They do not need matching IDs, but the card
must remain a truthful summary of the composed agent.

The test-only `evals/` directory accepts sorted
`<eval-set-id>.evalset.json` files and optional `test_config.json`. Files must
validate as ADK `EvalSet`/`EvalConfig`, each `eval_set_id` must match its
filename, and case IDs must be unique. Evals are validated during compilation but
never added to instructions or tools. Run all of them through the same authored
test entrypoint:

```bash
harnest test examples/self-serve/agents/helpdesk --evals
```

This always runs `tests/unit` first, then the validated eval sets in filename
order, automatically using `test_config.json` when present. Add `--smoke` to run
all three lanes. Smoke tests and evals are explicit live checks: they may call
the configured model or MCP services and consume credentials, time, and paid
capacity. Harnest runs each eval case once, uses a temporary compiled artifact,
and does not persist ADK eval history; retain CI output when a durable record is
needed.

Public resource files are compiled in deterministic filename order. A missing or
wrongly typed filename-matched export fails compilation with a convention error;
duplicate tool names, agent names, or identical MCP configurations also fail.
Explicit resources already present on the root definition are retained first,
then discovered resources are appended.

A subagent that needs its own filesystem-composed resources can use the nested
form `subagents/<name>/agent.py`. That file must export an `AgentDefinition`
named `<name>` and may have its own sibling `tools/`, `subagents/`, and
`mcp/` directories. Do not define both `subagents/<name>.py` and
`subagents/<name>/agent.py`.

The example's `mcp/knowledge.py` reads both `KNOWLEDGE_MCP_URL` and
`KNOWLEDGE_MCP_TOKEN`. It exports `knowledge = None` unless both are present, so
partial local or deployment configuration never constructs a broken MCP client.
When enabled, the definition retains `${KNOWLEDGE_MCP_URL}` and
`${KNOWLEDGE_MCP_TOKEN}` placeholders; expansion is deferred until ADK toolset
construction, so the token is not captured in the dataclass or its `repr`.

## Source and compiled artifact

Run the compiler explicitly when inspecting or integrating its output:

```bash
harnest compile examples/self-serve/agents/helpdesk \
  --output .harnest/helpdesk
```

The source folder remains the self-serve ownership boundary. The generated
folder contains a preserved `source/` tree, ADK-compatible `agent.py`,
`__init__.py`, and `__main__.py` adapters, the executable `harnest-agent`
launcher, mutable `server.yaml`, and `harnest-manifest.json`. ADK tools or the standalone launcher
consume the generated folder; authors edit only source. `.harnest/` is ignored
because the artifact is reproducible build output. The compiler also excludes VCS metadata,
virtual environments, caches, `.adk/`, `.harnest/`, `.env` files, and bytecode;
runtime secrets are injected by the engine rather than copied into artifacts.

`harnest plan orchestrator.py` applies the same zero-import experience to the
deployment declaration by injecting `AgentSource` and `define_orchestrator`.
