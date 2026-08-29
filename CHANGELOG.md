# Changelog

## Unreleased

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

## [0.4.1](https://github.com/Usefused/harnest/compare/v0.4.0...v0.4.1) (2026-08-29)


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
