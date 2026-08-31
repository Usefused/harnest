# Changelog

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
