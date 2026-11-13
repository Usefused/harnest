# Harnest codebase guide

This guide is for teams that change Harnest itself: contributors, and companies
running their own fork. It explains how the repository is laid out, how one
command travels through the Go CLI and the Python package, and where to make
common changes. To use Harnest rather than change it, read the public docs at
<https://usefused.com/docs/harnest>.

| Read | When you need |
| --- | --- |
| This page | The layout, the request path, and where each area starts |
| [files.md](files.md) | One line for every source file (generated, always current) |
| [adding-a-package.md](adding-a-package.md) | To ship your own code inside your Harnest build |
| [packs.md](packs.md) | To give your company its own branded command and agent defaults |
| [../frameworks.md](../frameworks.md) | To add or change an agent framework adapter |
| [../extensions.md](../extensions.md) | To add storage, sandboxes, or other Harnest Extensions |
| [../architecture.md](../architecture.md) | The detailed ownership and contract rules |
| [../development.md](../development.md) | Coding standards and the quality gates every change passes |

## Repository layout

| Directory | What it holds |
| --- | --- |
| `cmd/harnest/` | The `harnest` CLI, written in Go: commands, managed Python environments, scaffolding, packaging, hot reload |
| `cmd/harnest-agent/` | The small native launcher placed inside agent executables |
| `engine/` | Go library shared by the CLI and deployment tooling: load bundles, compile through Python, deploy |
| `internal/` | Go internals: `frameworks` (framework metadata), `runtimewheel` (the Python wheel embedded in release CLIs), `uvbootstrap` (embedded uv), `agentpack` and `agentlauncher` (agent executables), `agentscaffold`, `features` |
| `src/harnest/` | The Python package: the authoring API agents import, the compiler, the runtime, and the framework adapters |
| `studio/` | Harnest Studio: its Python server (`src/harnest_builder`) and editor frontend |
| `packages/` | Companion Python distributions released separately (`harnest-fused`, `harnest-threadify`) |
| `official-extensions/` | First-party Harnest Extensions (`docker`, `hatchet`, `rag`) |
| `schemas/` | JSON schemas for `config.yaml`, extension manifests, eval results, and other file formats. Go code reads them through the `schemas` package instead of keeping its own copies |
| `requirements/` | Known-good and minimum framework versions for the compatibility matrix |
| `examples/` | Sample agents and packs, several of them exercised by tests |
| `tests/python/` | Python tests; `suites.json` assigns each module to the unit, integration, e2e, or live tier. Go tests sit next to the code as `*_test.go` |
| `scripts/` | Build, release, and quality-gate scripts, including the generator for [files.md](files.md) |

## Two halves: Go owns processes, Python owns agents

The `harnest` binary is Go. It never imports agent code. It finds agent
folders, creates and locks each agent's Python environment with an embedded
uv, scaffolds files, packages artifacts and executables, and supervises the
server process.

Everything about agents is Python, in `src/harnest`: the API authors import
(`harnest.agent`, `harnest.graph`, `harnest.lifecycle`, ...), the compiler that
turns an agent folder into a framework-native application, and the runtime that
serves it. Compiler commands run through
`python -m harnest.cli <command>` inside the agent's environment
([cli.py](../../src/harnest/cli.py)). The CLI passes paths and flags and reads
JSON from stdout. Hot reload keeps one compiler process alive instead
([compiler_daemon.py](../../src/harnest/compiler_daemon.py)).

Shared metadata crosses the boundary in `src/harnest/frameworks/frameworks.json`
(framework names, packages, capabilities; the Go copy in `internal/frameworks`
must stay byte-identical) and the JSON schemas in `schemas/`.

## Follow one command: `harnest serve my-agent`

1. **CLI.** [commands.go](../../cmd/harnest/commands.go) `newServeCommand`
   loads `my-agent/config.yaml` with [engine/load.go](../../engine/load.go) and
   picks the agent's Python with [python.go](../../cmd/harnest/python.go). The
   environment is created or reused by
   [environment.go](../../cmd/harnest/environment.go), using the framework
   extras from `frameworks.json` and the hashed lock from
   [environment_runtime_lock.go](../../cmd/harnest/environment_runtime_lock.go).
2. **Compile.** The CLI runs `python -m harnest.cli compile my-agent --output
   <artifact>` (with `--reload`, [reload.go](../../cmd/harnest/reload.go) sends
   the same request to the compiler daemon instead).
   [bundle.py](../../src/harnest/bundle.py) `compile_artifact` calls
   `compile_application`, which:
   - receives the framework selected from `config.yaml` by the CLI and gets its adapter from
     [frameworks/registry.py](../../src/harnest/frameworks/registry.py);
   - imports the authored entrypoint (`agent.py:root_agent`);
   - in managed mode, composes the folders (`tools/`, `mcp/`, `skills/`,
     `subagents/`, `lifecycle/`, extensions) into one `AgentDefinition` or
     `Graph`, then calls `adapter.lower_managed(...)` to build the native ADK or
     LangGraph object; in advanced mode, validates the native object with
     `adapter.validate_advanced(...)`;
   - returns a `CompiledApplication`
     ([application.py](../../src/harnest/application.py)).
3. **Artifact.** `compile_artifact` writes a folder: `source/` (the agent's
   files), a generated `agent.py` and `__main__.py`, a `harnest-agent`
   launcher, `server.yaml`, and `harnest-manifest.json` (file hashes, framework,
   tasks, crons). The Go side checks the manifest
   ([engine/compile.go](../../engine/compile.go)).
4. **Start.** The CLI runs `<artifact>/harnest-agent serve`. That calls
   [runtime.py](../../src/harnest/runtime.py) `main`, which recompiles from the
   artifact's `source/` and builds the web app with `create_fastapi_app`.
5. **Runtime driver.** `_runtime_driver` asks the adapter for its driver
   (`adapter.create_runtime_driver`, e.g.
   [frameworks/adk/runtime.py](../../src/harnest/frameworks/adk/runtime.py)),
   then wraps it in framework-neutral layers: agent plugins, lifecycle hooks and
   extensions, credentials, external continuations, tasks. Every layer
   implements the `RuntimeDriver` protocol in
   [runtime_contract.py](../../src/harnest/runtime_contract.py).
6. **Transports.** [neutral_runtime.py](../../src/harnest/neutral_runtime.py)
   mounts the HTTP, SSE, and WebSocket routes. A2A and AG-UI live in
   `runtime_a2a.py` and `runtime_agui.py`. Each request goes through
   [runtime_invocation.py](../../src/harnest/runtime_invocation.py) to the
   driver, so every transport gets the same sessions, limits, and lifecycle.

`harnest run` takes the same path to step 3 and then invokes the agent once in
the terminal ([runtime_cli.py](../../src/harnest/runtime_cli.py)). `harnest
test` compiles the same way and then runs pytest and evals
([testing.py](../../src/harnest/testing.py)).

## Where each Python area starts

[files.md](files.md) lists every module by area. These are the entry points
worth reading first.

| Area | Start at | Owns |
| --- | --- | --- |
| Authoring API | `agent/__init__.py`, `graph.py`, `model.py`, `lifecycle.py` | Everything an agent author imports. Public names are frozen in `tests/fixtures/public-api.json` |
| Compiler | `bundle.py` | Folder discovery and composition, lowering through the adapter, artifact writing |
| Framework adapters | `frameworks/base.py` | All framework-specific code. Shared code never names a framework |
| Runtime | `runtime.py`, `runtime_contract.py`, `runtime_pipeline.py` | Driver wrappers, invocation flow, sessions, transports |
| Lifecycle and extensions | `lifecycle.py`, `extension_loader.py` | Decorator hooks and Harnest Extensions, discovered at compile time |
| Storage | `checkpoint.py`, `session.py`, `store.py` | Portable storage contracts and the built-in Postgres and Redis backends |
| Testing and evals | `testing.py`, `evaluation.py` | `harnest test`, the built-in evaluator, eval capture |
| Packs | `authoring/packs.py` | Harnest packs and branded CLIs; see [packs.md](packs.md) |

Two rules hold everywhere. Modules whose names start with `_` are private.
Public modules load their heavy dependencies lazily, so importing `harnest`
never imports a framework SDK.

## How files connect

[files.md](files.md) says what each file is for. To see how one connects to the
rest, ask the import graph:

```bash
python scripts/codebase_map.py deps bundle            # what bundle.py imports, and what imports it
python scripts/codebase_map.py deps frameworks/adk    # the same for a whole package
python scripts/codebase_map.py graph --json graph.json  # every Python import edge, for other tools
```

The graph includes imports inside functions, which Harnest uses widely to keep
startup light. For Go, `go list -deps ./cmd/harnest` shows package
dependencies; the CLI is one package, so its files call each other directly.

## Common changes

| Change | Where |
| --- | --- |
| Support a new agent framework | [../frameworks.md](../frameworks.md#adding-a-framework) |
| Add a `harnest` subcommand | A new `cmd/harnest/<name>.go` with `newXCommand`, registered in [root.go](../../cmd/harnest/root.go). If it needs agent code, add a subcommand to [cli.py](../../src/harnest/cli.py) and call it with `runPythonCLI`; [mcp.go](../../cmd/harnest/mcp.go) is a short example |
| Ship your own module or package with your build | [adding-a-package.md](adding-a-package.md) |
| Give your company a branded command and defaults | [packs.md](packs.md) |
| Add a storage backend, sandbox provider, or other plug-in | [../extensions.md](../extensions.md) |
| Add or change a public authoring name | Update `tests/fixtures/public-api.json` and the public Mintlify docs; the typing gate requires full annotations |
| Add a file format or config field | The JSON schema in `schemas/`, the Go loader in `engine/`, and the Python reader together |

## Before you open a change

- Give every new source file a module docstring (Python) or a comment above
  its `package` clause (Go), then run `python scripts/codebase_map.py`.
  `test_codebase_map` fails until [files.md](files.md) matches.
- Register new Python test modules in `tests/python/suites.json`.
- Run `make test` and `make quality`. The gates (complexity at most 10, full
  type annotations, ruff, public API snapshot) are described in
  [../development.md](../development.md).
- Add a line under `## Unreleased` in `CHANGELOG.md` for anything users can
  notice.
