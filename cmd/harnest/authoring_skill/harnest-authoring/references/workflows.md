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
harnest doctor
```

`init` refuses a non-empty destination. Treat generated source as editable
starter material; treat `.harnest/` as disposable build output. By default,
optional folders contain only ignored `_README.md` routing guides and the root
is one simple agent. Add `--example` only when the user wants working graph,
tool, plugin, skill, extension, eval, and test samples. Select
`--mode advanced` at initialization only for a new project that needs direct
framework APIs.

Before editing an existing project, inspect `config.yaml`, `agent.py`,
`instructions.md`, `agent-card.yaml`, and only the resource folders relevant to
the request. Preserve unrelated user changes. If multiple resources need the
same ordinary Python implementation, add it once under root `lib/` and import
it below `harnest.lib`; do not turn a helper into a discovered resource.

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
harnest test support-agent --smoke
harnest test support-agent --evals
harnest test support-agent --evals --eval-trajectory strict
harnest test support-agent --smoke --evals
```

- The default lane compiles first and runs `tests/unit/test_*.py` offline.
- When LiteLLM is installed, set `LITELLM_LOCAL_MODEL_COST_MAP=True` for a
  strictly network-silent offline lane; otherwise LiteLLM may attempt to refresh
  its public model cost map during import before falling back to bundled data.
- `--smoke` additionally runs live-runtime tests and may consume credentials,
  model tokens, MCP services, time, and money. Use it only when authorized.
- `--evals` runs validated eval assets after Python tests. ADK EvalSet JSON is
  ADK-specific; LangGraph can use authored pytest evaluations. Only root
  `evals/` assets are selected; nested eval files are not an executable lane.
  ADK scoring receives visible response parts and tool trajectory, never parts
  marked as hidden model thoughts.
- Eval trajectories default to `business`: required business calls must occur
  in order, while skill discovery and other extra calls are allowed. Use
  `--eval-trajectory strict` to require the exact authored call sequence.
- Placeholder-only test folders are valid and report that no Python tests were
  authored; compilation and opted-in evals still run.
- Test modules do not import Harnest or manually load artifacts. Compiler-owned
  fixtures provide `agent`, `tools`, and, for smoke tests, `client` and `smoke`.

## Compile and serve

```bash
harnest compile support-agent --output .harnest/support-agent
harnest serve support-agent --host 127.0.0.1 --port 8080
```

The compiled artifact contains its source, generated adapters, manifest, and a
small `harnest-agent` launcher. It runs without the external provisioner:

```bash
.harnest/support-agent/harnest-agent --host 127.0.0.1 --port 8080
```

The neutral server includes `/agent`, `/sessions`, `/responses`, and WebSocket
`/live`; streaming `POST /responses` uses SSE. `/openapi.json`, `/docs`, and
`/redoc` describe that surface in every mode. Only an advanced-mode ADK artifact
additionally exposes ADK-native endpoints.

Open `/` to test any compiled ADK or LangGraph agent in Harnest's neutral
playground. Create or select a session, inspect state, choose JSON response, SSE
streaming, or WebSocket live mode, and verify visible output plus tool activity.
The UI never calls framework-native endpoints. Bearer tokens stay in page memory
for HTTP/SSE; authenticated browser WebSockets require a same-origin cookie.

## Change checklist

Before finishing a modification:

1. Confirm every resource is in the right folder and follows its export-name
   contract.
2. Confirm nested agents own only their sibling supported resources; parent
   tools/skills do not leak in. Plugins and extensions are root-only, plugins
   contain only MCP clients plus skills, and extensions contain lifecycle
   behavior.
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
