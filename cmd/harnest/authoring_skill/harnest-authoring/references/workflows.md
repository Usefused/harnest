# Authoring workflows

Read this reference when initializing, validating, testing, compiling, serving,
or handing off a Harnest agent project.

## Diagnose installation

The release executable is a native Go CLI with its version-matched Python wheel
and native `uv` bootstrapper embedded inside it. Installation creates a separate
managed virtual environment for that wheel; users do not install Python or a
second Harnest package manually. The installer uses a compatible host Python
when one exists and otherwise installs pinned managed CPython into the Harnest
data directory. `HARNEST_BOOTSTRAP_PYTHON` opts out of that fallback and
requires the named host interpreter. After installation, use `harnest doctor`
to inspect the exact managed interpreter and packages selected by the CLI.
The runtime is private implementation state; do not activate it or invoke
`python -m harnest.cli` as the user workflow. If the visible command lacks
`init`, `serve`, `doctor`, or `skills`, rerun the current installer so it can
replace Harnest's retired Python launcher in place. Use `type -a harnest` to
identify any remaining non-writable or unrelated command collision.

## Initialize and inspect

```bash
harnest init support-agent --framework adk
harnest init support-graph --framework langgraph
harnest init direct-graph --framework langgraph --mode advanced
harnest init example-agent --framework adk --example
harnest env sync support-agent
harnest doctor
```

`init` refuses a non-empty destination. Treat generated source as editable
starter material; treat `.harnest/` as disposable build output. By default,
optional folders contain ignored `_README.md` routing guides, except for the
ignored resource guides, and the root is one simple agent. Add
`--example` for ignored code samples in otherwise guide-only managed folders.
The default agent and storage code stay unchanged. Copy or rename a sample to
its documented public filename to activate it; preserve required exports and
dependent resources. Skills, plugins, and evals use ignored native-format
examples rather than Python placeholders. Select
`--mode advanced` at initialization only for a new project that needs direct
framework APIs.

`env sync` uses the embedded `uv` and Harnest wheel to create an isolated,
fingerprinted environment below `.harnest/environments/`. It resolves
`pyproject.toml` and updates `uv.lock`; commit the lock after reviewing it. Do
not activate the environment or add Harnest, ADK, LangGraph, or framework
adapters as agent dependencies. Upgrade Harnest to change framework versions.
Harnest Extensions use this same interpreter and dependency set. Agent Plugin
MCP servers may use separate runtimes; their mutable dependencies and caches
belong in client-managed `PLUGIN_DATA`, not the immutable plugin source.
Compile, test, and serve run the same synchronization automatically. In CI, use
`harnest env sync AGENT_DIR --frozen` before testing to reject a missing or
stale lock instead of changing it.

Before editing an existing project, inspect `config.yaml`, any legacy `server.yaml`,
`agent.py`, `instructions.md`, `agent-card.yaml`, and only the resource folders
relevant to the request. Preserve unrelated user changes. If multiple resources need the
same ordinary Python implementation, add it once under root `lib/` and import
it below `harnest.lib`; do not turn a helper into a discovered resource.

## Upgrade an existing repository contract

Do not run `init` over an authored agent. After updating the Harnest CLI, first
plan its repository migration:

```bash
harnest upgrade support-agent
```

Planning is read-only. Review every create, rewrite, move, and manual blocker;
also inspect the working tree because the command does not interpret business
logic. Resolve blockers by hand. When the reported mutations preserve the
agent's intent, apply them explicitly:

```bash
harnest upgrade support-agent --apply
```

Apply prints its fresh effective plan, verifies source hashes before mutation,
and backs up all affected paths plus `plan.json` under
`.harnest/upgrade-backups/<id>/`. It
recognizes removed Harnest conventions such as `mcp_servers/`, legacy
`requirements.txt` manifests, filename-named MCP exports,
`Extension(...)` lifecycle aggregation, and old
framework-native extension exports. It adds missing current root contracts and
records the committed repository schema in `harnest.lock`. Ambiguous authored
code becomes a blocker instead of being guessed.

Review the resulting source diff; do not treat the backup as a substitute for
version control. Then run `harnest test AGENT_DIR` and compile it. Reinstall the
bundled coding-agent skill with `harnest skills install --force` only after
reviewing any customized project-local skill.

## Audit an advanced-mode migration

Do not run a generator over an agent that has already been edited. Before
switching an existing managed project to direct ADK or LangGraph APIs, run:

```bash
harnest mode advanced support-agent --check
```

The check only reports what needs attention. It does not change the configured
mode, rewrite `agent.py`, or move discovered resources. Review the report, then
convert the existing code deliberately using `Agent.advanced(...)` and explicit
framework imports. A portable `harnest.graph.Graph` does not require advanced
mode.

## Validate and test

```bash
harnest test support-agent
harnest test support-agent --no-output
harnest test support-agent --smoke
harnest test support-agent --evals
harnest test support-agent --evals --eval-output eval-result.json
harnest test support-agent --evals --eval-trajectory strict
harnest test support-agent --smoke --evals
```

- The default lane compiles first and runs `tests/unit/test_*.py` offline.
- MCP descriptors are compiled in that unit lane, but `MCPClientLifecycle`
  setup and remote discovery remain lazy. Unit-test gateway helpers without a
  network handshake.
- When LiteLLM is installed, set `LITELLM_LOCAL_MODEL_COST_MAP=True` for a
  strictly network-silent offline lane; otherwise LiteLLM may attempt to refresh
  its public model cost map during import before falling back to bundled data.
- `--smoke` additionally runs live-runtime tests and may consume credentials,
  model tokens, MCP services, time, and money. Use it only when authorized.
  Put real gateway/MCP handshakes here and assert tool behavior rather than a
  fixed number of adapter-created HTTP clients.
- `--evals` runs validated ADK EvalSet assets against either ADK or LangGraph
  after Python tests. The optional `test_config.json` can select any installed
  ADK metric, judge/user-simulator configuration, or `customMetrics` function.
  Only root `evals/` assets are selected; nested eval files are not an executable
  lane. ADK scoring receives visible response parts and tool trajectory, never
  parts marked as hidden model thoughts. LangGraph scoring adapts neutral final
  responses and tool events to the same evaluator and preserves multi-turn
  sessions; use smoke tests for live bidirectional media or multimodal inputs.
  This is a portable ADK-format lane, not a LangSmith dataset/experiment run.
  A complete structured JSON `EvalRunResult` prints by default. It contains full
  cases, actual and expected invocations, overall and per-invocation metrics
  including rubric details, and session details. `--eval-output FILE`
  atomically writes that same payload to the selected file. Harnest keeps no
  implicit result history, so retain the file as a CI artifact when needed.
- Eval trajectories default to `business`: required business calls must occur
  in order, while skill discovery and other extra calls are allowed. Use
  `--eval-trajectory strict` to require the exact authored call sequence.
- Placeholder-only test folders are valid and report that no Python tests were
  authored; compilation and opted-in evals still run.
- `--no-output` suppresses terminal output from unit, smoke, and eval test
  execution while preserving the command's exit status. An explicitly selected
  `--eval-output` file is still written.
- Test modules do not import Harnest or manually load artifacts. Compiler-owned
  fixtures provide `agent`, `tools`, and, for smoke tests, `client` and `smoke`.
  The smoke fixtures share one compiled server lifecycle and close its stores
  once after the selected suite. Do not close the injected client or stores.
  Omit `session_id` for an isolated single-turn check. Reuse an explicit ID for
  intentional multi-turn behavior, and keep it unique to that test because the
  development `MemoryStore` preserves state across the suite.

## Compile and serve

Enable local invocation in `config.yaml`:

```yaml
spec:
  interfaces:
    cli: true
```

```bash
harnest compile support-agent --output .harnest/support-agent
harnest run support-agent "Summarize the open incidents"
printf '%s\n' 'Continue' | harnest run support-agent --session incident-session
harnest serve support-agent
```

`harnest run` requires the explicit CLI interface, compiles ephemerally, and
invokes the root agent inside the same runtime process without an HTTP endpoint.
Positional input is passed to the generated launcher through stdin rather than
process arguments. Use
`--output text|json|ndjson`; `--session` opens an existing session owned by the
local runtime identity. One-shot runs can execute queued tasks but do not claim
cron schedules. Process-local approvals and client tools require a serving
transport and fail closed in local mode.

The compiled artifact contains its source, generated adapters, manifest, and a
small `harnest-agent` launcher. It runs without the external provisioner:

```bash
.harnest/support-agent/harnest-agent serve
printf '%s\n' 'Hello' | .harnest/support-agent/harnest-agent run
```

Author optional server overrides under root `server` in `config.yaml`; omit
unchanged settings. The compiler emits a full `server.yaml` beside the launcher,
which reads it at startup. Legacy authored `server.yaml` still works, but cannot
coexist with inline settings. Replace the compiled copy and restart for an
operator override. Use it for
host/port, remote-bind consent, timeout, concurrency, request size, and the
playground toggle. Add `server.live: true` in authored `config.yaml` to enable
WebSockets on the same listener; new projects otherwise use HTTP/SSE only.
The playground disables its Live choice when `/agent` does not advertise it.
It does not configure authentication, persistent sessions,
TLS, secrets, or deployment resources. The request-size limit covers all HTTP
bodies and WebSocket frames. Explicit serve flags are temporary overrides.

Use an exact `${NAME}` value when deployment should supply a setting through the
environment. Harnest preserves it during compilation, resolves it at startup,
and validates the result as the field's declared type. Do not use `$NAME` or
partial interpolation. A missing, empty, or invalid variable stops startup
without printing its value.

The neutral server includes `/agent`, `/sessions`, `/responses`, and WebSocket
`/live`; streaming `POST /responses` uses SSE. Approval-protected work returns
`requires_action` and continues the exact suspended task through
`POST /approvals/{approvalId}` after an `approve` decision. It does not rerun
the invocation or replay earlier side effects; later protected calls request
their own approval. `deny` and expiry never execute the action. The bundled
store and suspended tasks are process-local, so restart invalidates outstanding
development approvals. `/openapi.json`, `/docs`, and
`/redoc` describe that surface in every mode. Only an advanced-mode ADK artifact
additionally exposes ADK-native endpoints.

During an active `/live` response, send `response.cancel` with the exact
`responseId` returned by `response.created`. Harnest waits for managed
framework/tool cleanup, returns `response.completed` with `status: cancelled`,
and keeps the socket available for the next `response.create`.
For SSE, abort or close the HTTP request instead; SSE has no client-to-server
frame channel, and the closed stream cannot carry a terminal acknowledgement.

Open `/` to test any compiled ADK or LangGraph agent in Harnest's neutral
playground. Create or select a session, inspect state, choose JSON response, SSE
streaming, or WebSocket live mode, approve or deny protected actions, and verify
visible output plus tool activity.
Select **Evals** to discover and run root `evals/*.evalset.json` suites locally,
choose business or strict tool trajectories, and inspect case, metric, response,
and tool-call evidence in the browser.
The UI never calls framework-native endpoints. Bearer tokens stay in page memory
for HTTP/SSE; authenticated browser WebSockets require a same-origin cookie.

## Change checklist

Before finishing a modification:

1. Confirm every resource is in the right folder and follows its export-name
   contract.
2. Confirm nested agents own only their sibling supported resources; parent
   tools/skills do not leak in. Harnest Extensions, agent-plugins, and root
   lifecycle hooks are root-only. An Agent Plugin uses `plugin.json` with optional
   `mcp.json` and skills; a Harnest Extension has `extension.yaml`, `extension.py`, and
   declared capabilities.
3. Confirm all `harnest.*` names are explicitly imported and sibling discovered
   resources are not manually registered. Confirm reusable helpers live only in
   root `lib/`, need no `__init__.py`, and are imported below `harnest.lib`.
4. After a rename, move, or deletion, confirm graph strings, tests, skills,
   evals, export names, and declared identities no longer reference the old
   resource. Confirm every discovered graph resource is consumed.
5. Keep secrets out of source, skills, logs, cards, and compiled artifacts.
6. Run focused tests, `harnest test`, and a compile. Run smoke/evals only when
   relevant and authorized.
7. Report the selected framework/mode, checks run, any live external calls, and
   any remaining provider-specific requirements.
