# Framework adapters

Harnest compiles, serves, and evaluates agents written for an underlying agent
framework (today Google ADK and LangGraph). Each framework is one adapter.
Harnest owns everything that would be the same for every framework; the
adapter only translates between Harnest's contracts and the framework's native
API. ADK and LangGraph are registered exactly the way a new framework would be.

This is a repository engineering reference for contributors. Public authoring
documentation lives in the Mintlify `harnest/` docs.

## Ownership

The test: if two frameworks would implement it identically, Harnest owns it;
if it needs the framework's own types, the adapter owns it.

| Harnest owns | Adapters own (translation only) |
| --- | --- |
| Authoring model: `Agent`, `Graph`, folder conventions, `config.yaml` | Lowering a definition to native objects (`lower_agent`, `lower_graph`) |
| Portable lifecycle hooks and when they run | Wiring model hooks in as a native extension (`model_lifecycle_extension`) |
| Server and transports: `/responses`, live, AG-UI, A2A, playground, auth | The `RuntimeDriver` that runs the native objects |
| State: sessions, checkpoints, assets, credentials, stores | Native storage wrappers and checkpoint protocol (`managed_checkpointer`) |
| Tasks, cron, durable tools, continuations | The native suspend primitive (`suspend_durable`) |
| Token policy, MCP governance, output policy, telemetry, audit | Message shapes for token policy; MCP tool objects |
| Compile, bundle, manifest, lock, env sync, CLI, packaging | Model objects for `LiteLLMModel` (`build_litellm_model`) |
| Eval runners and reporting (the ADK evaluator is one runner) | Exposing an agent to the evaluator (`run_builtin_evals`, `evaluation_runtime`) |

Managed mode keeps Harnest's ownership `FULL` almost everywhere. Advanced mode
hands model, tool, and MCP calls inside the user's native code to the
framework (`WRAPPED_ONLY`) and checkpoints to native storage
(`FRAMEWORK_OWNED`); see `lifecycle_coverage.py`.

## Where things live

| Concern | Location |
| --- | --- |
| Release-owned metadata (names, distributions, version ranges, requirements, doctor modules, capabilities) | `src/harnest/frameworks/frameworks.json` (canonical) and `internal/frameworks/frameworks.json` (byte-identical Go copy) |
| Adapter contract | `src/harnest/frameworks/base.py` (`FrameworkAdapter`, `Capability`, errors) |
| Registry | `src/harnest/frameworks/registry.py` (`get_framework`, `framework_names`, `_ADAPTERS`) |
| ADK adapter, implementation, and public native types | `src/harnest/frameworks/adk/` |
| LangGraph adapter, implementation, and public native types | `src/harnest/frameworks/langgraph/` |
| Go metadata access | `internal/frameworks` (`Lookup`, `Known`, `Choices`, `Supports`) |
| Per-framework `harnest init` starter files | `cmd/harnest/framework_scaffolds.go` |

Inside each framework package, `adapter.py` is the entry point and the other
modules are that framework's implementation, reached with lazy imports. The
package `__init__` exports only the framework-specific types authors use
directly, loaded lazily so importing them never imports the SDK:

- `harnest.frameworks.adk`: `ADKStore`, `ADKSessionStorage`
- `harnest.frameworks.langgraph`: `LangGraphStore`

Framework-neutral public names cover everything else: `lifecycle.framework_extension`
for framework plugins or middleware, `ModelConnector.build()` and
`resolve_model(model)` for models, `Graph.build()`,
`bundle_agent(anchor, agent)`, `MCPClient.connect()`, `MCPClient.connection_options()`,
`FrameworkStorage` for framework-specific storage in shared types,
`create_fastapi_app(session_store=..., framework_session_storage=...)`, and
`load_framework_app()`.

Compiler selection is bound before importing authored modules and restored on
success or failure. Public builders consult that task-local context, then the
active invocation context. They never guess a framework outside Harnest.
Nested and concurrent compilations retain independent selections; copied
compiler contexts expire when compilation finishes. `Agent.build()` follows
the same contract as model and graph builders. Advanced graphs adapt Harnest
storage with `store.framework_checkpointer()`; no framework package import is
required for Harnest-owned storage.

## The adapter contract

Adapters subclass `FrameworkAdapter` and must stay import-light: import the
SDK inside hook bodies. Each hook's docstring in
`src/harnest/frameworks/base.py` says when Harnest calls it, what it receives,
and what to return; the module docstring lists the hooks in call order.

Required hooks, the minimum needed to compile and serve one managed agent:

| Hook | Purpose |
| --- | --- |
| `lower_agent` | Lower a managed `Agent` definition to the framework's runnable root |
| `validate_advanced` | Accept an `Agent.advanced(...)` export, or raise `AdvancedTargetError` |
| `build_litellm_model` | Build the framework's LiteLLM adapter for one `LiteLLMModel` |
| `create_runtime_driver` | Return a `RuntimeDriver` (see `runtime_contract.py`) for a compiled application |
| `framework_extension_type` | Type that `@lifecycle.framework_extension` factories must return |
| `model_lifecycle_extension` | Adapt portable model hooks into one native extension |
| `is_user_turn`, `is_instruction`, `reduce_tool_result` | Message shape for token policies |
| `mcp_tools` | Project discovered MCP tool schemas into framework tools |

Optional hooks either default to a neutral no-op (`wrap_managed`,
`prepare_advanced`, `managed_checkpointer`, `validate_advanced_checkpointer`,
`credential_extensions`, `observability_defaults`, `validate_framework_extensions`,
`is_suspension`,
`accepts_framework_session_store`, `host_sessions`, `evaluation_runtime`,
`is_injected_tool_context`, `is_framework_code_executor`) or raise
`UnsupportedFrameworkFeature` naming the missing feature (`lower_graph`,
`build_framework_server`, `suspend_durable`, `run_builtin_evals`,
`playground_eval_agent_module`, and `attach_advanced_extensions` when
extensions exist).

Class attributes describe behavior shared code reads directly:
`framework_extension_label`, `framework_session_store_label`,
`framework_code_executor_label`, `resume_reenters_tool`
(does a resumed durable tool re-enter the Python tool frame, or resume the
model loop?), and `retains_live_tool_frame`.

Capabilities in `frameworks.json` declare optional behavior callers check
before using it. `test_framework_adapters.py` fails if a framework declares a
capability without overriding its hooks:

| Capability | Meaning | Hooks |
| --- | --- | --- |
| `graphs` | Managed `Graph` roots can be lowered | `lower_graph` |
| `framework-api-server` | Advanced apps are served by the framework's own HTTP API | `exposes_framework_server`, `build_framework_server` |
| `framework-session-service` | Hosts may inject framework-specific session storage | `host_sessions`, `accepts_framework_session_store` |
| `root-agent-subagents` | A managed root agent consumes `subagents/` directly; also enables `harnest add subagent` | (compiler and CLI only) |
| `evals` | `harnest test --evals` and playground evals work | `run_builtin_evals`, `playground_eval_agent_module` |
| `generate-content-config` | `Agent(generate_content_config=...)` reaches the framework's model; without it the compiler warns and the field is ignored | (compiler only; `lower_agent` passes the field to the model) |

## Adding a framework

1. Add an entry to `src/harnest/frameworks/frameworks.json` and copy the file
   to `internal/frameworks/frameworks.json`. Add a matching optional-dependency
   extra in `pyproject.toml` (framework requirements plus
   `shared_runtime_requirements`). Tests on both sides fail if they drift.
2. Create `src/harnest/frameworks/<name>/` with `__init__.py` (lazy exports of
   any native types authors need), `adapter.py`, and the implementation
   modules. Implement the required hooks, then the hooks your capabilities
   promise. Reuse the shared helpers instead of copying another adapter:
   `lazy_exports` (`frameworks/lazy.py`) for the package's `__getattr__`,
   `sdk_isinstance` for "is this an SDK object" checks that must not require
   the SDK, `AdvancedTarget.named` for advanced target names,
   `require_framework` for framework-name validation, and
   `harnest.testing._run_eval_suite` for `run_builtin_evals`.
3. Register the adapter class in `_ADAPTERS` in `registry.py`.
4. Add a `frameworkScaffolds` entry in `cmd/harnest/framework_scaffolds.go`
   with at least an `advanced` starter that writes `agent.py`
   (`TestEveryFrameworkHasAScaffold` enforces this).
5. Add a durable-store schema migration. The Postgres schema
   (`store_postgres_schema.py`) constrains `framework` with a `CHECK` list,
   so bump `SCHEMA_VERSION` and migrate existing databases
   (`test_postgres_schema_accepts_exactly_the_registered_frameworks` fails
   until this is done).
6. If authors need new native types, export them from the framework package
   and add them to `tests/fixtures/public-api.json`. No shared public API
   changes are needed: native extensions, models, graphs, and storage already
   go through framework-neutral names.
7. Add driver and lowering tests, register new test modules in
   `tests/python/suites.json`, and add the framework to
   `.github/workflows/framework-compatibility.yml`. Pin its distribution in
   `requirements/frameworks.txt` (known-good) and
   `requirements/frameworks-minimum.txt` (lowest supported);
   `test_compatibility` checks both stay inside the range in `frameworks.json`.

The fixture framework in `tests/python/test_framework_adapters.py` shows the
minimum adapter: it registers through the same metadata and registry paths,
then runs a message through the real runtime pipeline without edits to shared
code.

## Enforced boundaries

`test_framework_adapters.py` enforces two rules:

- No module outside `harnest/frameworks/` imports a framework package
  (`harnest.frameworks.adk`, `harnest.frameworks.langgraph`, ...). Shared code
  goes through `get_framework(...)`.
- No module outside `harnest/frameworks/` imports a framework SDK, except
  Harnest's built-in eval engine (`evaluation.py`, `eval_metrics.py`,
  `eval_model_transport.py`, `eval_errors.py`). That engine is ADK's
  evaluation library; it evaluates agents from every framework and is not the
  ADK framework adapter. Eval files (`*.evalset.json`, `test_config.json`)
  follow its schemas.

Shared code that needs to recognize a framework's native objects asks every
registered adapter: `is_injected_tool_context` (approval arguments) and
`is_framework_code_executor` (sandbox providers).

One authoring field is still ADK-specific: `Agent(generate_content_config=...)`
passes Google GenAI generation settings. Only frameworks that declare
`generate-content-config` (today, ADK) use it. For any other framework,
the compiler warns which agents set it and lowers them without it;
compilation still succeeds.
