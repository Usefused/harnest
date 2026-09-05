# Changelog

## [0.14.0](https://github.com/Usefused/harnest/compare/v0.13.0...v0.14.0) (2026-09-05)

### Features

* Emit provider-exposed ADK reasoning and LangGraph reasoning blocks as
  portable `thinking` events, and normalize agent or graph-node lifecycle into
  `agent_activity` events. Centralize provider-reported model, provider, finish
  reason, and exact input/output/total token counts in typed `agent_metadata`
  events, aggregate usage on completed responses, and allow an explicit
  `OutputPolicy(agent_metadata="raw")` opt-in for JSON-safe native metadata.
  Carry these events and optional agent attribution through local responses,
  JSON, SSE, WebSocket, streaming A2A tasks, privacy-safe traces, and the
  playground without treating reasoning as final answer text. Raw metadata is
  excluded from traces and remains disabled by default. Persist a bounded,
  versioned public completion snapshot before a durable run becomes terminal so
  any replica can return the same per-call metadata, aggregate usage, caller
  metadata, and structured result. Raw provider metadata remains ephemeral
  unless `persist_raw_agent_metadata=True` is also explicitly selected.

* Add invocation-scoped, default-deny `AgentRuntimePrincipal` grants for server,
  client-hosted, and MCP tools. When a principal is active, capabilities without
  a permission tag are unavailable. Custom HTTP routes and local agent sessions
  can pass `agent_principal=` after application authentication; nested calls
  inherit or narrow grants, queued tasks reconstruct them from private durable
  state, and cron-originated agent calls without explicit authority start with
  no capabilities. ADK and LangGraph omit unavailable capabilities where Harnest
  owns the model boundary and recheck Harnest-owned capabilities before
  execution. Managed topologies fail before startup when Harnest cannot
  guarantee complete projection; advanced-mode enforcement is best effort and
  does not rewrite user-owned graph or tool wiring.

* Make every neutral HTTP, SSE, and WebSocket response pollable through
  `GET /responses/{id}` within its exact user and session scope. Live clients
  can now submit `approval.decision` on the active WebSocket and receive an
  `approval.resolved` acknowledgement before execution resumes; the existing
  HTTP approval endpoint remains available as a fallback. Managed runtimes
  retain completed response envelopes durably when backed by `HarnestStore`,
  while advanced custom runtimes provide best-effort process-local recovery.

* Add provider-enforced sandbox network policies for no-network,
  unrestricted, and exact-host allowlist modes, with optional destination-port
  and private-network controls. Require portable providers to declare typed
  enforcement capabilities and fail closed before execution when a requested
  guarantee is unavailable. Add `sandbox.provider` authority for Harnest
  Extensions that implement these providers.

* Add `harnest plugins install SOURCE --project AGENT_DIR` for safely installing
  reviewed local Agent Plugins 1.0 packages. Validate manifests without running
  package code, reject links and special files, stage copies atomically, and
  require `--force` for explicit replacement. Keep Harnest Extension discovery
  under the separate `harnest extensions` namespace.

* Add `harnest extensions init NAME` and `harnest extensions install SOURCE
  --project AGENT_DIR` workflows for local source trees or verified public PyPI
  wheels. Validate and materialize packages without importing their code, pin
  downloaded releases through extension-owned dependency metadata, preserve
  packaged root READMEs, and install through staged replacement. Collect the
  Fused-maintained Docker and Hatchet packages under `official-extensions/`,
  with substantive PyPI descriptions, isolated tests, buildable wheels,
  explicit official-project detection, and token-free PyPI Trusted Publishing.

* Resolve the embedded Harnest wheel, framework, project, Harnest Extension,
  task, and transitive dependencies into one committed, hash-verified
  `harnest-runtime.lock`. Install only from that resolution and make frozen sync
  reject missing or source-stale locks before dependency installation.

### Changed

* Make `harnest env sync` maintain an IDE-discoverable `.venv` link to the
  current managed environment. Retarget the link atomically after dependency
  changes, recreate it on cached syncs, and preserve user-owned `.venv` paths
  with the exact managed interpreter as a fallback.

* Declare the complete Python runtime as a PEP 561 typed distribution and add
  IDE-visible documentation and annotations to every reviewed public callable.
  Ship editor stubs for the official Docker and Hatchet extension namespaces.
  The release gate now verifies the typed marker, metadata, signatures, hover
  documentation, and extension stubs in the embedded wheel's public API.

* Remove the built-in `Sandbox.container()` API and Docker SDK dependency from
  Harnest core. Install `harnest-extension-docker` and use
  `harnest.extensions.docker.docker.sandbox(...)` instead. Docker extension
  `0.2.0` now owns container startup, identity-scoped reuse, budgets, network
  modes, deadlines, bounded output, and cleanup behind the provider-neutral
  `Sandbox.provider(...)` contract.

### Fixes

* Serialize concurrent framework-state and application-data writes on each
  PostgreSQL session lease so asyncpg never receives overlapping commands on
  one connection. Add optional `lease_pool_options` to isolate long-lived
  session lease connections from checkpoint and other short store operations.

* Harden both official extensions and their release path. Bound Docker daemon
  calls by the all-in sandbox deadline, preserve phase-specific startup failures,
  use a separate bounded cleanup window, disable image-defined health checks,
  and label managed containers. Bound Hatchet JSON payloads, disable implicit
  dotenv discovery, cap recovery-client concurrency, and retain durable waits
  with backoff across transient provider outages. Pin publishing actions to
  reviewed commits and reject extension release tags outside `main` history.

* Support operator-configured SOCKS proxies in Harnest-owned MCP HTTP clients
  while preserving standard environment proxy inheritance. Keep proxy URLs and
  credentials out of client-construction diagnostics.

### Features

* add agent runtime principals ([c88822d](https://github.com/Usefused/harnest/commit/c88822d8628af0245f63dd229a72af0c2cc1a69c))
* complete Python SDK typing ([8cdd1f9](https://github.com/Usefused/harnest/commit/8cdd1f9ccfdea25abe8c1eaf7406753ad95d8066))
* **extensions:** add official Hatchet extension ([3677e87](https://github.com/Usefused/harnest/commit/3677e87ff9c8535d8f8553701a46eb8972f095c4))
* extract official sandbox extensions ([ecaf74f](https://github.com/Usefused/harnest/commit/ecaf74f8d33b6c6ff9e801a3b0e5fd6b1d6a173f))


### Fixes

* **extensions:** keep Hatchet install layout valid ([4174d1c](https://github.com/Usefused/harnest/commit/4174d1c6acce4522cd23c03dbcb7d189cef1250f))
* harden agent principal projection ([c886212](https://github.com/Usefused/harnest/commit/c886212ac49c797855b45a77272af8c395235820))
* isolate and serialize postgres session leases ([a65e8b4](https://github.com/Usefused/harnest/commit/a65e8b4458d8e72eb35f42fdc4429ca88d7ab3ec))
* persist task grants and clarify extension limits ([382cd81](https://github.com/Usefused/harnest/commit/382cd81d17105132e68ff3b16690c2387b23e9f9))
* propagate agent principal authority ([a728583](https://github.com/Usefused/harnest/commit/a7285836c447714958aaf0956eb9699530a974e2))

## [0.13.0](https://github.com/Usefused/harnest/compare/v0.12.1...v0.13.0) (2026-09-04)

### Fixes

* Group public Python imports under their domains: authentication, HTTP, server
  configuration, tools, MCP, models, lifecycle, context, runtime, assets, and
  storage. Preserve existing imports and type identity; add `lifecycle.coverage`
  and update authoring examples to avoid implementation-module imports.

* Expose sandbox scopes through `from harnest.sandbox import control`, with
  `control.execute(...)`, `control.cleanup(...)`, and `control.current()`.
  Add `control.cleanup(timeout_seconds=5)` for provider resource release after
  cancellation or invocation revocation. Require a finite cleanup deadline, block
  new sandbox execution inside cleanup, and use its remaining budget for built-in
  Docker removal requests.

* Isolate recoverable nested sandbox helper failures and local timeouts to the
  failed scope and its descendants, so callers can catch them without cancelling
  healthy outer or sibling work. Explicit cancellation and managed-context
  revocation remain call-wide.

* Add a hardened Chrome browsing sandbox example with an explicit host allowlist,
  pinned Playwright image, fresh-container scope, and bounded resources.

### Features

* expose stable public APIs and scoped sandbox cleanup ([c85daed](https://github.com/Usefused/harnest/commit/c85daed65ea7ec6490391870462744d4e734aafe))

## [0.12.1](https://github.com/Usefused/harnest/compare/v0.12.0...v0.12.1) (2026-09-04)

### Fixes

* Keep nested sandbox helper timeouts local to their scope instead of permanently
  shortening the outer operation's deadline. Preserve absolute ancestor limits,
  worker deadlines, shared cancellation, and managed-context revocation.

### Fixes

* scope nested sandbox deadlines without shortening outer operations ([e39cc92](https://github.com/Usefused/harnest/commit/e39cc922f5742a377e9868cf7a1b07141d2a7c42))

## [0.12.0](https://github.com/Usefused/harnest/compare/v0.11.1...v0.12.0) (2026-09-04)

### Changed

* Load Agent Plugins 1.0 packages from `plugin.json`, with independently optional
  skills and declarative `mcp.json`. Map portable MCP servers into ADK and
  LangGraph with bounded validation, isolated component failures, safe path and
  origin handling, and persistent per-installation `PLUGIN_DATA` across reloads.

* Separate reusable Harnest Extensions in `extensions/<name>/` from application
  hooks and factories in `lifecycle/`; Agent Plugins remain in `plugins/`.
  Add `extension.yaml`, `extension.py`, `Extension`, `ExtensionContext`, and
  `context.extensions`, with backed-up, collision-checked `harnest upgrade`
  migration and legacy runtime-plugin compatibility. Update generated projects,
  dependency discovery, and extension search without changing runtime ownership.

- Lock the resolved framework version in `harnest.lock` after environment sync, enforce it during installation and compilation, and preserve it through schema upgrades.
- Remove `Agent(sandbox=...)`; use named `sandboxes=[...]` grants and authored tools on both frameworks.
- Run container sandboxes through a framework-independent Docker provider with explicit execution/invocation/session scopes, bounded container reuse, CPU/memory/process/scratch budgets, non-root read-only execution, and typed execution status. Scratch is cleared after every call.
- Continuously test pinned, minimum, and latest-compatible ADK/LangGraph dependencies, including shared evaluations and real Docker isolation.

### Features

* Opt into WebSocket serving with `server.live: true` in `config.yaml`. New
  projects default to HTTP/SSE only; legacy server files retain live access
  unless disabled. Disabled live transport rejects native and custom WebSocket
  handshakes and is unavailable in playground transport selection.

* Configure standalone serving through an optional `server` section in
  `config.yaml`, with defaults for omitted settings. New projects and upgrades
  no longer create `server.yaml`; existing files remain supported, while
  conflicting inline and legacy settings fail before agent code loads.

### Fixes

* Accept composed FastAPI HTTP routers on versions that retain included-router
  wrappers. Validate fully prefixed paths at every include depth while retaining
  reserved-path, duplicate-endpoint, and unsupported-route protections.

### Features

* support portable agent plugins and harden application runtime ([6041858](https://github.com/Usefused/harnest/commit/6041858e9eb018468c690778f8e3acfb5f17b6ce))

## [0.11.1](https://github.com/Usefused/harnest/compare/v0.11.0...v0.11.1) (2026-09-04)

### Fixes

* Establish managed invocation context for native ADK CLI and playground
  evaluations so skill tools and session-aware callbacks can run. Keep eval
  sessions separate from live sessions, preserve runtime capability ownership,
  and explain unscored execution errors separately from task-quality failures.
  Revoke context on cancellation and early stream closure; reject unsafe native
  legacy-workflow `before_run_callback` replacement responses with repair guidance.

## [0.11.0](https://github.com/Usefused/harnest/compare/v0.10.1...v0.11.0) (2026-09-03)

### Features

* Support framework-neutral sandbox providers on managed ADK and LangGraph,
  with multiple named `sandbox/<name>.py` declarations explicitly assigned through
  each agent's `sandboxes` allowlist and called from authored tools through
  `context.sandboxes`, with provider request/result metadata and lazy Docker
  execution. Assignments do not expose automatic model tools. Folder discovery never grants access;
  subagents declare their own permissions. Preserve explicit legacy
  `Agent(sandbox=...)` and native ADK provider compatibility;
  isolation and CPU/memory limits remain provider-owned.
* Fill guide-only managed project folders with opt-in code samples when using
  `harnest init --example` on ADK or LangGraph. Samples remain ignored until
  renamed, including native-format skill, plugin, and eval examples; preserve
  the default agent and storage code without adding redundant examples.
* Return a complete structured JSON eval result from `harnest test --evals` by
  default, and add `--eval-output FILE` for atomically writing the same result
  to a selected file, including full case, invocation, metric, rubric, and
  session details. Continue through independent scored suite failures, retain
  partial results and redacted error details for infrastructure failures, and publish a
  versioned result schema.
* Standardize generated agents and implicit eval judge/simulator models on
  `OPENAI_MODEL`, `OPENAI_API_KEY`, and `OPENAI_BASE_URL`, including custom
  OpenAI-compatible endpoints. Add `LiteLLMModel.from_openai_environment()`;
  explicit eval model overrides and native-provider connectors remain supported.
* Reuse an agent's explicit LiteLLM transport, custom authentication, and
  lifecycle hooks for compatible eval judges and text user simulators in CLI
  and playground runs. Preserve eval model overrides, reject ambiguous client
  choices, and keep borrowed clients under their original runtime ownership.

### Fixes

* Explain feature-folder authoring errors in plain language, including the
  expected layout, missing Python declarations, and concrete steps to repair
  misplaced files or keep unused examples out of discovery.
* Harden container sandbox execution with host-side deadlines, bounded streaming
  stdout/stderr (1 MiB by default, configurable with `max_output_bytes`),
  cancellation-aware admission, and failed-start cleanup. Reject revoked
  invocation contexts instead of falling back to anonymous execution; discard
  aborted containers before reuse. Stop detached processes between successful
  calls while preserving files, without adding session isolation or CPU/memory limits.
* Continue ADK sandbox model turns after successful code execution when the
  provider returns `STOP`, preserving genuine empty-response failures and
  native retry behavior instead of ending without a final answer.
* Preserve explicitly supplied provider SDK clients and nested `model_kwargs`
  credentials in LangGraph model calls and evaluation transport discovery.
* Keep long tool identifiers and payloads inside the playground layout, reveal
  complete identifiers on expansion, and wrap large tool results on desktop
  and mobile.

### Features

* add tool-invoked sandbox capabilities and authoring examples ([508f55b](https://github.com/Usefused/harnest/commit/508f55bd69d198bb64a231837ef7c8ef6118d672))
* improve evaluation results and model transport configuration ([117471a](https://github.com/Usefused/harnest/commit/117471a3428790ffdd7979ff799cc3596c68d9e1))

## [0.10.1](https://github.com/Usefused/harnest/compare/v0.10.0...v0.10.1) (2026-09-01)

### Fixes

* Route A2A cancellation through durable task ownership after the originating
  process-local A2A producer has finished.

### Fixes

* **a2a:** route durable task cancellation ([108f86e](https://github.com/Usefused/harnest/commit/108f86e986127cda5aedf35f5cefffabc5400ee9))

## [0.10.0](https://github.com/Usefused/harnest/compare/v0.9.0...v0.10.0) (2026-09-01)

### Features

* Add bidirectional A2A 1.0 support: authored HTTP+JSON and JSON-RPC bindings
  now expose direct messages, streamed and asynchronous tasks, scoped task
  lookup/listing, cancellation, subscriptions, and approval continuations. Add
  a lazy outbound client plus `RemoteAgent` graph/tool adapters with explicit
  streaming, polling, credential, timeout, and network-authority policy. A2A
  task snapshots now use the configured Harnest Memory, PostgreSQL, or Redis
  store. With a durable store, `@task` waits recover, reconcile, subscribe, and
  cancel through the same owner-scoped run, continuation, and Procrastinate
  task state across process restarts and replicas, and cancellation responses
  retain their terminal state after the A2A event queue has drained.
* Preserve A2A Agent Card icons, extensions, security schemes and requirements,
  and JWS signatures through strict bundle loading and compilation.

### Fixes

* Include the task runtime in the `all` development extra so the documented
  full quality gate can exercise compiled tasks against PostgreSQL.

### Features

* **a2a:** add durable task support ([626e044](https://github.com/Usefused/harnest/commit/626e044b44c4ad96e3d1058da72692b3121bc7e6))


### Fixes

* **ci:** install compiled task runtime ([645429f](https://github.com/Usefused/harnest/commit/645429f768733627a4a7e1cbc63a97c7e480dd57))

## [0.9.0](https://github.com/Usefused/harnest/compare/v0.8.0...v0.9.0) (2026-09-01)


### Features

* **cli:** add PyPI plugin search ([afb7ca8](https://github.com/Usefused/harnest/commit/afb7ca8123413a9398f0fb93aa0a5117eb272ea5))


### Fixes

* **playground:** restore addressable sessions ([cb4d511](https://github.com/Usefused/harnest/commit/cb4d5116010092e6ae08d268d35f0624d0dd2974))

## [0.8.0](https://github.com/Usefused/harnest/compare/v0.7.0...v0.8.0) (2026-09-01)

### Features

* Add `harnest plugins search` for cached public-PyPI discovery without a
  Harnest-operated registry. Results are restricted to digest-verified wheels
  with the Harnest entry-point and runtime-plugin bundle contract, package code
  is never imported, and only explicitly approved Fused package names receive
  an official label.
* Add folder-discovered UTC cron schedules for queued tasks, task-scoped
  `context.agent` sessions, and explicitly enabled local root-agent invocation
  through `harnest run` or the generated launcher without an HTTP endpoint.
  Generated launchers use explicit `serve` and `run` commands, and task workers
  now stop cleanly when their enclosing server cannot bind.
* Run validated ADK EvalSet suites against LangGraph through the neutral runtime,
  including multi-turn state, tool trajectories, every installed ADK metric,
  judge and user-simulator criteria, and authored `customMetrics` functions;
  discover, run, and inspect those local suites in the playground; CLI evals
  print detailed metric results by default and support `--no-output` across
  unit, smoke, and eval test lanes.

### Fixes

* Restore Playground transcripts from shareable `?session=` URLs, keep session
  selection addressable, and add bounded session-ID search with exact lookup.
* Let live WebSocket clients cancel the active response by ID, await framework
  cleanup, receive a terminal `cancelled` response, and reuse the same socket.

### Features

* expand agent runtime workflows ([801c755](https://github.com/Usefused/harnest/commit/801c7553848b001259c5b93825a5f73583c5ebdd))

## [0.7.0](https://github.com/Usefused/harnest/compare/v0.6.0...v0.7.0) (2026-09-01)

### Fixes

* Clear conversation-owned messages and tool state when the playground creates
  or switches sessions, while preserving the first turn during implicit session
  creation.
* Preserve the playground's streamed chronology by rendering each assistant
  segment around its tool calls, collapse tool details by default, mark
  unfinished calls as failed when a request errors, and shorten the visible
  heading to `Playground`.
* Reject undeclared managed-tool arguments before ADK or LangGraph can silently
  discard them, returning value-free repair guidance to the model.
* Rank filesystem skills with deterministic fuzzy name and description matching,
  preserve ranked dynamic-source candidates, and use one bounded catalog
  fallback when a model query returns no skills.

### Performance

* Reduce the framework-neutral invocation overhead by indexing lifecycle hooks,
  reusing immutable tool pipelines, and bypassing empty plugin-context work
  without changing result canonicalization.

### Features

* add development agent reload ([02e327c](https://github.com/Usefused/harnest/commit/02e327c02a1bed2645fde17b64f18cb68c2efe33))
* harden skill and tool execution ([42ac7f9](https://github.com/Usefused/harnest/commit/42ac7f9189a434f3a7e0a079a2c9554994a1582e))

## [0.6.0](https://github.com/Usefused/harnest/compare/v0.5.0...v0.6.0) (2026-08-31)


### Features

* add runtime skill sources ([10bf8ad](https://github.com/Usefused/harnest/commit/10bf8ad85ded8353f0b99b5ff6c9f1577d3a1405))


### Fixes

* expose skill descriptions to LangGraph agents ([058965d](https://github.com/Usefused/harnest/commit/058965df3d1d743c8cd6294e587dcb7952e2c56c))
* preserve managed framework execution options ([5dfa41a](https://github.com/Usefused/harnest/commit/5dfa41a9ce667348e08dd294ba5eb40ddd25d905))


### Refactoring

* remove obsolete skill loader code ([ca0e65c](https://github.com/Usefused/harnest/commit/ca0e65c42f637108794e73cad195659cd6d5ad67))

## [0.5.0](https://github.com/Usefused/harnest/compare/v0.4.1...v0.5.0) (2026-08-30)


### Features

* add runtime plugins and durable execution ([276df43](https://github.com/Usefused/harnest/commit/276df43c60104076184809ed9dc90468bf3108a2))
* add typed multimodal media contracts ([cb00755](https://github.com/Usefused/harnest/commit/cb007557d3f79b04c4d615b6a08e4285975c8995))


### Fixes

* finalize authored changelog notes before tagging ([c51b696](https://github.com/Usefused/harnest/commit/c51b696c47b61eca50a1454f9e6e870175218d2d))
* preserve Python 3.10 runtime compatibility ([055d9dc](https://github.com/Usefused/harnest/commit/055d9dcc95ec8ea47474e7cf9379bc5ddf9e00ec))
* scope checkpoints and subagent model hooks ([ecdce19](https://github.com/Usefused/harnest/commit/ecdce19148f7a4a23487b626e4c1dbf5d645993a))

## [0.4.1](https://github.com/Usefused/harnest/compare/v0.4.0...v0.4.1) (2026-08-29)

### Harnest agent runtime integrations

This release makes Harnest a more complete framework-neutral runtime for ADK
and LangGraph agents, while preserving access to framework-native capabilities
when applications need them.

### Runtime architecture

* Consolidated session resolution, input validation, limits, and non-streaming
  execution in one `InvocationCoordinator` shared by neutral responses and
  custom HTTP routes without changing their public contracts.
* Added a validated `RuntimeCapabilities` boundary and one explicit runtime
  wrapper pipeline while preserving existing `CompiledApplication` attributes.
* Split response continuations, SSE, WebSocket, session wire handling, and
  driver contracts out of the neutral router to reduce cross-feature coupling.

### Portable lifecycle and contexts

* Added one framework-neutral lifecycle kernel for agent, model, tool, MCP, and
  HTTP execution. Hooks explicitly return `Next(...)` to continue or
  `Finish(...)` to stop, and implicit `None` returns fail closed.
* Every lifecycle phase supports integer `order` values. Lower values execute
  first, with plugin dependency/source order and source location providing a
  deterministic tie-break. The structural ordering between lifecycle stages
  remains runtime-owned.
* Added managed ADK and LangGraph adapters so native model, tool, MCP, and
  subagent activity crosses the same portable lifecycle boundaries exactly
  once.
* Expanded invocation context into typed, revocable views for agent identity,
  resources, credentials, sessions, storage, assets, MCP clients, and runtime
  plugins. Credential authority remains a separate context so secrets do not
  enter general resources, logs, traces, or public errors.
* Session context can read and update durable state without sending that state
  through the model. Asset and custom-storage views retain named routing while
  default asset selection remains explicit and modality-aware.

### Storage registry

* Added distributed lifecycle declarations for session stores, checkpoint
  authorities, named asset stores, and named custom stores. Definitions may
  live in separate extension files and are compiled into one typed
  `StorageRegistry` without requiring a central connection file.
* A shared factory may fulfil several storage roles, allowing one connection or
  pool to be reused without duplicate startup, shutdown, or hidden resources.
* Added portable session/checkpoint adapters for ADK and LangGraph while keeping
  framework-native advanced-mode ownership explicit.
* Storage starts before plugin and extension resources and stops after them.
  Partial startup unwinds only resources that entered their lifecycle, in
  reverse order, without masking the original failure.

### Runtime plugins

* Added application-owned `RuntimePlugin` compilation under
  `plugins/<name>/plugin.yaml`, distinct from manifestless MCP-and-skill
  agent-plugins. Runtime plugins use the agent's interpreter, dependency lock,
  process, event loop, and compiled artifact rather than a sidecar runtime.
* Runtime plugins may declare static PEP 621 dependencies in their own
  `pyproject.toml`. Environment synchronization resolves those constraints
  together with the root application before compiler imports, while preserving
  one shared interpreter instead of installing a plugin-private environment.
* Plugins are exposed through `harnest.plugins.<name>`, declare a singleton
  `plugin`, and may provide typed per-invocation contexts, tools, MCP clients,
  skills, subagents, lifecycle extensions, and application-scoped resources.
* Plugin dependencies determine namespace import and startup order; shutdown is
  reversed. Concurrent runtimes cannot independently start or prematurely stop
  the same process-global plugin singleton.
* Plugin manifests declare a closed capability set. The compiler validates
  contributions before import, and the Go engine binds dependency-ordered
  plugin provenance into the verified artifact digest.
* Managed ADK and LangGraph compose declared plugin content automatically.
  Advanced mode retains Harnest-owned lifecycle and context boundaries but
  rejects content that would silently alter a native application.
* Added privacy-safe `plugin_mutation(...)` OTEL auditing and live compiled-agent
  coverage for LangGraph with a local FastMCP subprocess and ADK with its native
  runner.
* Added the `context.continuations` plugin capability for external durable
  runtimes. Provider-bound startup and invocation ports persist opaque waits
  with framework resume identity and deterministic schema validators. Once the
  native checkpoint and provider outcome are both durable, any replica sharing
  the stores may atomically claim and resume the run.
* Added principal/session-scoped `GET /responses/{responseId}` polling for
  `in_progress`, completed, and payload-free failed responses. Continuations
  use Memory, PostgreSQL, or Redis Harnest stores with indexed provider lookup,
  compare-and-swap transitions, bounded reconciliation, and privacy-safe OTEL
  mutation auditing.
* Response polling now reconstructs opaque waiting and terminal boundaries from
  shared run/session state, so requests handled by a different replica converge
  without a process-local future. Terminal responses are retained as bounded
  tombstones and never drift to a later session turn.
* Added a no-tool Hatchet runtime-plugin example, an independently authored ADK
  consumer agent, and a Docker Hatchet Lite/PostgreSQL worker fixture. Stopping
  the Harnest plugin cancels local monitors but never owns or cancels Hatchet
  jobs; a new replica keyset-pages pending continuations and restores their
  provider monitors with application recovery authority.
* Added a gated live journey that calls a real LiteLLM provider, executes the
  consumer-owned Hatchet tool exactly once, and uses one PostgreSQL-backed
  Harnest store for sessions, checkpoints, and external continuations. Replica
  A stops while the job is pending; replica B reconciles it, resumes the exact
  ADK tool call, and restores the completed transcript from that database.

### Durable tools and queued tasks

* Added the `@tool(durable=True)` compiler/runtime foundation for asynchronous
  managed tools. Harnest lowers these tools to ADK's long-running tool type or
  LangGraph's checkpointed interrupt identity and supplies replay-stable native
  correlation to plugin adapters. ADK resumes its model loop with the exact
  persisted `FunctionResponse`; LangGraph re-enters the checkpointed tool node,
  so external submission keys are deterministic and replay-safe.
* Added strict `tasks/<name>.py` discovery and the framework-neutral `@task`
  authoring API for queued, scheduled, retryable service work. Direct calls
  remain ordinary Python calls; `.defer(...)` returns an opaque handle for
  status, cancellation, and JSON-safe persisted results. Awaiting an unfinished
  handle from `@tool(durable=True)` uses the same cross-replica continuation
  protocol as runtime plugins.
* Procrastinate is compiler-owned and conditionally installed only when a
  public task export exists. Its PostgreSQL queue lifecycle, worker ownership,
  retries, and user/agent mutation audits remain separate from framework
  checkpoint resumability.

### Live verification

* Live-verified the official Hatchet SDK against a Docker Hatchet Lite worker,
  including compiled ADK suspension, external completion, and transcript
  persistence.
* Live-verified cross-replica recovery with a real OpenAI model and PostgreSQL:
  replica A stopped while Hatchet work was pending, replica B reconciled the
  continuation, resumed the exact ADK function response, made the second model
  call, and completed the original session.
* Live-verified the pinned Procrastinate worker against PostgreSQL, including
  delayed scheduling, retry, persisted task results, and payload-redacted logs.

### Sessions and transcripts

* Session records now consistently expose `id`, `userId`, `state`, `createdAt`,
  `updatedAt`, and `metadata`.
* Added `GET /sessions/{id}/messages` for reading an ordered, portable
  transcript without discarding ADK events or LangGraph message metadata.
* Framework-specific values remain available under namespaced `adk` or
  `langgraph` metadata instead of leaking into the neutral response shape.
* Session and transcript listings are bounded to 100 records by default and at
  most. Both support `limit` and opaque `cursor` pagination and always return
  `nextCursor`, including `null` on the final page.
* Pagination cursors are scoped to the authenticated user and resource.
  Transcript cursors tolerate appended messages but reject malformed,
  cross-session, cross-user, or stale cursors.

### Structured agent contracts

* Agents now support explicit Pydantic input and output schemas, and portable
  graphs support an output schema. Contracts can live in the authored
  `models/` package instead of being repeated across agents and transports.
* Tools and client tools can validate structured, serializable results through
  return annotations or an explicit `output_schema`.
* ADK now leaves portable request validation with Harnest so multipart input is
  not flattened and rejected at the native node boundary. Validated Pydantic
  tool results are serialized without ADK's additional `result` envelope.
* Added `FrameworkMetadata[T]` so an application can deliberately request
  native turn metadata in its output model. Harnest does not inject that
  metadata when the schema has not opted in.
* Added a complete runtime-metadata example covering structured graph output,
  session records, and transcript retrieval.

### Multimodal content

* Added strict portable `Text`, `Image`, `Audio`, `Video`, `File`, `AssetRef`,
  and typed `Data[T]` Pydantic parts. Users configure accepted MIME types,
  byte size, dimensions, pixels, duration, animation, pages, and related limits
  with reusable `Annotated` constraints rather than `server.yaml`.
* Inline base64 is now the default transient media policy. For media consumed
  as model input, Harnest validates and leases decoded bytes before framework
  persistence, injects them only into the immediate model call, and excludes
  them and private lease identifiers from checkpoints, history, logs, traces,
  audits, intermediate public events, and session messages.
* Applied transient media handling to top-level structured input, typed local
  tool output, and mid-turn client-tool results, including retry-safe subagent
  model calls in ADK and LangGraph. Native framework history persists only
  content-free attachment placeholders, never private lease identifiers.
* Final inline media output is returned once on its authenticated response
  transport. Durable or replayable output requires an explicit `Stored(...)`
  policy.
* Added explicit `Stored(...)` field metadata for durable media, named
  `@lifecycle.asset_store(name=...)` factories, model-call-time signed URLs,
  and scoped `context.assets` access. Retention, path, and URL expiry are
  authored in the Pydantic contract rather than `server.yaml`.
* Ordinary tools using `Stored(...)` output are explicitly asynchronous so
  storage can be awaited without changing synchronous direct-call behavior.
* Retained authenticated session asset upload, range download, metadata,
  deletion, inspection, and lifecycle-owned storage contracts. JSON, SSE,
  WebSocket, structured output, and session messages share the same portable
  shapes while public transcript projections remain content-free.

### Telemetry export

* Added the public `TelemetryExporter` contract and repeatable
  `@lifecycle.telemetry_exporter` factories.
* A single agent can export traces and logs to multiple independent OTLP
  destinations, with per-destination signal selection.
* Exporters are created lazily at runtime, shared by the root application and
  its subagents, and flushed and closed with the application lifecycle.
* Standard OpenTelemetry environment configuration remains supported and can
  be used alongside authored exporters.
* Added a live multi-collector test that verifies trace and log fan-out for both
  ADK and LangGraph.

### Authentication and credentials

* Added authentication lifecycle extensions and a dedicated Harnest
  authentication authoring skill.
* Added a root-owned credential-provider lifecycle for resolving downstream
  credentials during an active invocation.
* Credential resolution is scoped to the current identity and session, with
  native ADK credential integration and lifecycle cleanup.

### Custom HTTP endpoints

* Added `@lifecycle.http_routes` for mounting application-owned FastAPI routes
  from the root agent's `extensions/` directory.
* Route factories receive an `AgentInvoker` that invokes the compiled root
  agent through Harnest's authentication, sessions, lifecycle, credentials,
  limits, tracing, approvals, and client-tool continuation flow.
* Custom routes work across managed ADK and LangGraph applications and advanced
  runtimes, appear in OpenAPI, and are rejected at compilation when they
  conflict with Harnest-owned or other custom routes.

### MCP, tools, and approvals

* MCP clients now participate in the compiled application lifecycle, including
  lazy connection setup and deterministic cleanup.
* Added `request_human_approval(...)` for async tools and native callables that
  need to evaluate risk before protecting only a specific operation. Approval
  resumes the same task without replaying the evaluation.
* Added a standalone live HTTP probe covering compilation, server startup,
  suspension, approval, and completion of a dynamically protected operation.
* MCP approval policies can target selected tools instead of requiring approval
  for every operation exposed by a server.
* Approval decisions remain bound to the exact identity, session, tool, and
  arguments and work across JSON, SSE, and WebSocket invocations.

### Custom libraries and RAG-ready integrations

* Authored `lib/`, `models/`, tools, and lifecycle extensions can be composed
  into custom retrieval and generation workflows without a Harnest-specific RAG
  implementation.
* Resource, credential, MCP, and telemetry factories are discovered during
  compilation but invoked lazily by the runtime, so compilation does not require
  a live database or network connection.
* Structured tool boundaries reject unsupported output shapes early, making
  custom retrieval results portable across ADK, LangGraph, and Harnest
  transports.

### Fixes

* target current repository for releases ([a3da0a6](https://github.com/Usefused/harnest/commit/a3da0a6af2b5777ae231aff7b25eefca996ba3dd))

## [0.4.0](https://github.com/Usefused/harnest/compare/v0.3.0...v0.4.0) (2026-08-28)


### Features

* expand and stabilize portable runtime ([f94f613](https://github.com/Usefused/harnest/commit/f94f613813fc258b12bbaccc302e5480c18e6f07))
* support dynamic human approval ([b30f96d](https://github.com/Usefused/harnest/commit/b30f96da24144d1630a451348a034d4d4ed3a83b))

## [0.3.0](https://github.com/Usefused/harnest/compare/v0.2.0...v0.3.0) (2026-08-28)


### Features

* expand agent runtime integrations ([221a7b8](https://github.com/Usefused/harnest/commit/221a7b842806f1d83538a3331696d00bc707ed48))


### Fixes

* package published releases independently ([f37f6b5](https://github.com/Usefused/harnest/commit/f37f6b59572abc80cda7d1794d63aa1cb5e20543))

## [0.2.0](https://github.com/Usefused/harnest/compare/v0.1.21...v0.2.0) (2026-08-28)


### Features

* add configurable agent output policy ([b2cf2ca](https://github.com/Usefused/harnest/commit/b2cf2ca836157646469d6ff7d629e7f50e79c50a))
* refine playground appearance controls ([239f974](https://github.com/Usefused/harnest/commit/239f974e47787b89ad2c1118fe573e4b137c34c5))


### Fixes

* preserve shared stores across smoke tests ([03dede0](https://github.com/Usefused/harnest/commit/03dede0e82b7a6072bbd1989c398428db265b180))
