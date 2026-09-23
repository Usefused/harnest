# Changelog

## [1.1.0](https://github.com/Usefused/harnest/compare/v1.0.0...v1.1.0) (2026-09-23)

- Add `harnest add eval NAME` with an interactive `--i` picker, built-in metric
  presets, and bare custom scoring stubs; existing evaluation settings are preserved.
- Show elapsed-time activity during eval setup and execution, with flushed stage
  messages, suite progress and timings, plain-text CI heartbeats, and quiet-mode support.
- Add `harnest.evaluation.metric`, `MetricContext`, and `MetricScore` for sync
  or async custom scoring services, with per-turn or conversation scoring and
  retained evidence. Native ADK metrics continue to work in the same eval pipeline.
- Prevent release preparation from failing on empty pull-request output when
  Release Please has no proposal to return.
- Point runtime API and task-storage help links to the current documentation site.
- Require Harnest `>=1.0.0,<2` for the official Docker, Hatchet, and RAG
  extensions (versions 0.4.2, 0.2.2, and 0.1.3 respectively).
- Route AG-UI through the existing response-stream runner so request limits,
  cancellation, timeouts, and response-status tracking match `/responses`.

### Features

* share response execution across AG-UI and native transports ([3284c66](https://github.com/Usefused/harnest/commit/3284c66056b51db6c56055d8e6a4a1e5a8784390))


### Fixes

* align extension release checks and bump patch versions ([3765da5](https://github.com/Usefused/harnest/commit/3765da5eb359ce66e74c927a4d0209bb37388def))
* handle empty release-please pull request output ([a86eedd](https://github.com/Usefused/harnest/commit/a86eeddbdad7d6e20f0e6fddaade9a7b63627400))
* require Harnest 1.x in official extensions ([997f51c](https://github.com/Usefused/harnest/commit/997f51c20cf19d89a62a7212bfa94ab57095431c))
* update documentation links to usefused.com ([eb1caf0](https://github.com/Usefused/harnest/commit/eb1caf0bf2b02a2975f559743c3e504465fcf846))

## [1.0.0](https://github.com/Usefused/harnest/compare/v0.23.0...v1.0.0) (2026-09-23)

### Breaking changes

* Require Python 3.11 or newer across Harnest, Studio, bundled integrations, and
  extension scaffolds. Python 3.10 projects and bootstrap interpreters are no
  longer supported. New agents and the managed runtime continue to use Python
  3.12; remove the Python 3.10 TOML and enum compatibility fallbacks.
  `harnest upgrade --apply` migrates existing Python 3.10 runtime declarations
  and compatible project requirements to 3.11, with the normal review, backups,
  and stale-source checks. Agent commands refresh the changed environment.

### Features

* Serve agents over the [AG-UI](https://ag-ui.com) protocol at `POST /agui`,
  translating the same neutral runtime events used by `/responses` and
  `/live` into AG-UI's `RUN_STARTED`/text-message/tool-call/`STATE_DELTA`/
  `RUN_FINISHED` event stream. Disable it per app with `agui_enabled=False`
  on `create_neutral_app`/`create_neutral_router`. A mid-run human approval
  or client-tool suspension reports `RUN_ERROR`, since AG-UI's base protocol
  has no human-in-the-loop primitive; use `/responses` or `/live` for agents
  that require approvals. LangGraph applications that return an authored
  `Event.state_delta` now also stream a neutral `state_delta` event (SSE
  `response.state_delta`) that AG-UI renders as a `STATE_DELTA` JSON Patch;
  ADK does not yet emit it, since its native per-event state channel is not
  safely public.

* Add compile-only optional dependency selection in `harnest-compile.yaml`,
  maintainable through project packs. Compile runtime code, standard agent files,
  and complete skills/extensions/plugins while leaving teammate documentation and
  other project-only files out of artifacts. Preserve whole dependency packages
  and shared-runtime deduplication, and emit file/package inclusion and size reports.

* Scaffold a company project pack with `harnest pack init NAME`, generating a
  runnable Python CLI, editable `harnest-compile.yaml`, and sample team documentation.
  Copy documentation and binary assets unchanged with `context.files.from_file`,
  retaining pack ownership and managed migration checks. Let pack CLIs and planners
  initialize from a Harnest template with an optional SHA-256 pin before applying
  pack customizations, preserving staged previews and template-owned settings.

* Add opt-in project packs and an embeddable company CLI for typed init options,
  staged core/pack upgrade plans, versioned migrations, generated-file ownership,
  conflict detection, stale-plan rejection, and backed-up application with rollback.

* Build native agent executables with attached or explicitly embedded Python
  runtime packs. Resolve shared agent dependencies together and deduplicate
  identical dependency files across packs and launch caches. Verify runtime
  identity and platform before execution; launch without installed Harnest,
  Python, or startup dependency downloads. Support Linux, macOS, and Windows
  `.exe` builds, including relocatable dependency console tools, shared Windows
  hardlinks, and child-process cleanup on launcher termination.

* Add the optional `harnest-threadify` package for session-linked business
  telemetry, native Threadify access, and filtered OTLP/HTTP Protobuf export.
  Keep ADK/LangGraph plumbing, payloads, exception text, and unselected tool
  activity out of this destination. Add committed-session observers and HTTP
  request scopes that unwind after streaming or cancellation.

### Fixes

* Keep long Studio workspace paths truncated with a hover tooltip instead of
  displaying a horizontal scrollbar.

* Renew private authentication only for accepted client-tool results and approval
  decisions. Concurrent resumptions retain each active request's authority;
  failed validation and cancellation cannot revive expired credentials or expose
  them in continuation data.

* Preserve ordinary tool JSON with list- or object-valued `type` fields, including
  JSON Schema unions, when inspecting media. Prevent the asset-content callback
  from failing with `unhashable type` while retaining real attachment detection.

### ⚠ BREAKING CHANGES

* require Python 3.11 and migrate legacy projects on upgrade

### Features

* add company project packs and authoring CLI ([1f260da](https://github.com/Usefused/harnest/commit/1f260dad38d5188d526b3dafc15dda9415fd700f))
* add compile dependency selections and team pack scaffolding ([32dda66](https://github.com/Usefused/harnest/commit/32dda66a9658ca8d5f7285efd4923c148f3ea6de))
* add optional Threadify business telemetry integration ([d2bd590](https://github.com/Usefused/harnest/commit/d2bd5904f0f6b53eecfe1fa296c1363420bdda7b))
* compile native agents with deduplicated runtime packs ([bef9f73](https://github.com/Usefused/harnest/commit/bef9f7342903f41ca985f7e7820189188d6a268a))
* forward templates through project pack initialization ([8496f2a](https://github.com/Usefused/harnest/commit/8496f2a3142c0681ce5f8e095621bb1adb7b5394))
* require Python 3.11 and migrate legacy projects on upgrade ([9752172](https://github.com/Usefused/harnest/commit/9752172a54c86c1a588f1cee122c71309a05ba65))


### Fixes

* handle structured type fields in asset callbacks ([8592d20](https://github.com/Usefused/harnest/commit/8592d201e8c78290e210581ea2c9c52d4370da81))
* ignore retired agent settings and harden continuation auth ([0647369](https://github.com/Usefused/harnest/commit/0647369585c047699b935ac2e144d7f43908768e))

## [0.23.0](https://github.com/Usefused/harnest/compare/v0.22.0...v0.23.0) (2026-09-22)

### Features

* Add opt-in `Agent(skill_selection=DecisionSkillSelector(...))` for ADK and
  LangGraph. Build batched decisions from visible skill metadata, use the
  existing `SkillContext` for task/state input callbacks, and load only selected
  pinned versions. Support multiple or no selections, typed discovery/error
  fallbacks, bounded requests, and invocation-scoped reuse without sending
  conversation history or skill bodies to the decision provider by default.

* Remove the embedded Studio tab and its browser assets from Playground. Keep
  conversation testing and evaluation editing in Playground; use the standalone
  `harnest studio` for agent authoring and MCP setup. Existing Studio view URLs
  return to the conversation while preserving the selected session.

* Add provider-neutral typed decisions with custom asynchronous providers,
  versioned questions, validated choice/score/probability results, explicit
  routing and threshold policies, bounded evaluation, offline fixtures, and
  privacy-safe telemetry. Publish decision registries through lifecycle/context
  resources and evaluate them with invocation-scoped `context.decisions` in ADK
  and LangGraph. Provider integrations remain optional. Include a runnable Jev
  support-triage example with a pinned SDK/model, explicit offline fixtures,
  and manual-review outcomes for uncertain or failed decisions. Follow Jev
  classification with a configurable LLM reply while preserving the validated
  routing fields internally; offline mode replaces both provider calls with fixtures.
  Keep decision results private by default; opt in with
  `OutputPolicy(decision_results=True)` for separate decision events across
  public output and streaming transports. The Jev example exposes only its reply
  by default. Keep synthetic graph-to-model inputs out of restored conversation
  transcripts in ADK and LangGraph.

* Add reviewed MCP connections and Fused orchestration to Agent Builder Studio,
  sharing the playground's OAuth and Admin backend. Visual controls and Build
  with AI use the same discovery and provisioning plans, with browser-bound
  OAuth, explicit operation selection, revision checks, and private project
  execution credentials. Keep credentials out of model context and source;
  never replay uncertain remote mutations.

* Disable deployment by default behind `HARNEST_ENABLE_DEPLOYMENT=true`. Hide
  Studio's deployment controls and provision CLI help until explicitly enabled;
  enforce the flag for Studio APIs, provisioning, and Go deployment execution.

* Bundle Fused Admin and Fused Auth skills with Studio's compiled builder agent,
  covering real MCP provisioning APIs, service discovery, OAuth/PKCE, and separate
  management and execution credentials instead of invented server packages.

* Connect generated Fused webhook receivers to durable Harnest channel workers.
  Persist admission before acknowledging delivery, isolate agent/actor sessions,
  and queue threaded Slack replies through SDK-scoped Engine execution. Recheck
  allowlists before processing and sending; retain ambiguous execution/delivery
  failures without automatically replaying effects. Include an authenticated
  Slack acceptance agent and PostgreSQL restart/concurrency coverage.

* Bundle Harnest Studio's web server, browser assets, and compiled assistant in
  release CLI artifacts. Start it with `harnest studio`; use the current folder
  by default or select another with `--workspace`. Cache Studio's isolated,
  release-matched runtime and verify packaged launches in CI on Linux, macOS,
  and Windows. Discover nested agent projects recursively from the workspace,
  show relative folder paths in project selectors, and pick up added agents on
  refresh while excluding generated folders, environments, and symbolic links.

* Recover Studio AI proposals from transient model/connection failures with one
  bounded retry. Report specific credential-safe failure categories and a log
  reference instead of attributing every runtime failure to provider setup.
  Let the model correct unavailable tool calls instead of aborting source-reading
  requests, and discard failed private runtimes to clear retained sessions. Include
  existing deployment configuration in initial AI context and keep storage changes
  consistent with deployment dependencies.
  Recover missing skill-reference lookups with bounded corrective feedback, and
  document separate in-memory task storage in the bundled authoring guidance.
  Expose source requests as a native builder tool while retaining Studio's source
  permissions, read limits, and revision-checked proposal review.

* Stop generating resources and scaling blocks in agent config.yaml and remove
  them from the runtime configuration model. Silently ignore existing values,
  including empty, null, or invalid legacy hints, without applying them to server
  settings or Studio deployment discovery. Use harnest-deployment.yaml for
  workload limits and replicas, and server.http for explicit HTTP limits.

* Generate reviewable Studio deployment configuration from agent environment and
  service usage, with editable images, memory/CPU, services, and network settings.
  Ground Build with AI in the deployment schema and authoring guidance; support
  separate multi-document Kubernetes YAML and explain invalid manifest formats.
  Add per-workload host aliases and DNS for local and Kubernetes deployments.

* Run Studio Build with AI through a private compiled Harnest agent grounded in
  the release’s authoring and authentication skills. Production wheels include
  the compiled server; development compiles it on demand. Keep proposal review,
  source-access limits, revision checks, and provider settings intact.

* Delete capabilities from the Agent Builder inspector with a revision-checked
  preview of affected files and possible references. Keep complete source packages
  in project-local recovery storage and restore them from Deleted capabilities
  without overwriting newer files.

* Preserve command-output text selections during polling and add per-line and
  full-output copy controls in Agent Builder.

* Show highlighted line diffs in Agent Builder proposal previews, with added and
  removed lines, original/new line numbers, and expandable unchanged context.

* Clear the Agent Builder prompt when sending, preserve subsequent drafts while
  the model responds, and restore failed submissions when the composer is untouched.

* Reconnect every Agent Builder canvas edge by dragging its line or endpoints,
  with destination validation before source changes. Move tools, MCP connections,
  whole skill packages, subagent branches, and category batches between managed
  agents; reject cycles, root-only assignments, stale edits, and collisions.
  Preserve supporting assets and roll back incomplete moves.

* Group Agent Builder architecture components into expandable categories and
  package folders, with per-project expansion state and direct source access.
  Simplify canvas cards with clean disclosure chevrons and direct edge dragging,
  and provide automatic arrangement while retaining expanded branches.

* Add configurable agent and service provisioning through `harnest provision`
  and the Agent Builder Deploy menu. Run images locally with Docker Compose or
  in an existing Kubernetes/K3s namespace, connect external databases and Redis,
  override dependencies per environment, and retain persistent data during removal.
  Track numbered deployment revisions and optional release labels, pin image
  identities, and roll back successful snapshots with current credential references
  while preventing implicit persistent-service downgrades.
  Guide Studio deployment with a configuration review, readiness progress, local
  endpoint links or Kubernetes port-forward commands, and selectable rollback
  history. Distinguish planned access from running agents and show missing inputs
  before deployment.


* Add an independent local Agent Builder with a Harnest capability palette,
  drag-and-drop canvas, source-backed graph connections, revision-checked code
  editing, and reviewable LLM proposals. Project initialization, capability
  scaffolding, builds, tests, and serving run through the Harnest CLI. Browse local
  folders to open agents or choose new-project destinations, and manage installed
  extensions with catalog search, local/PyPI installation, source configuration,
  and dependency synchronization.
  Let the assistant read related project source when enabled and regenerate edits
  from actual file contents, preserving review and revision checks instead of
  failing when a requested change needs an unselected file.

### Fixes

* Release Docker 0.4.1, Hatchet 0.2.1, and RAG 0.1.2 extension packages with
  Harnest `>=0.21.3,<0.24` compatibility, including the 0.23 series.

* Pass LangGraph agent-node replies and structured results to downstream graph
  nodes instead of leaving the predecessor's input as their output.

* Clarify sensitive-data handling in authentication and incident-triage skills:
  use synthetic credentials for authoring tests, restrict runtime credential
  use to trusted destinations, and omit supplied secrets and personal details
  from triage outputs, tool calls, and specialist handoffs.

* Preserve Agent Builder browser connections across refreshes with an authenticated
  session cookie, provide in-page reconnection, and block project creation until
  the workspace loads to prevent null-path errors.

* Initialize Agent Builder creation fields before opening the dialog and retain
  submitted values independently of dialog changes while initialization runs.

* Allow flat Python subagents during runtime dependency discovery, so agents
  created with `harnest add subagent` can synchronize their environments.

* Connect Studio to Fused with temporary OAuth client credentials, browser consent,
  and PKCE. Configure the Engine URL without a per-user registration key.

### Features

* add decision-based skill selection and streamline Playground ([b76167c](https://github.com/Usefused/harnest/commit/b76167c5447a37caf081fa82c88f94c6d3992d2a))
* add Studio Fused orchestration and durable channels ([6e54093](https://github.com/Usefused/harnest/commit/6e54093e33aaff9b11d77f1680e4fa14bbc87f7c))
* add typed decisions and Jev workflows ([8a39f8e](https://github.com/Usefused/harnest/commit/8a39f8e69a1c3ab2ed3046a265b1609028ba59a3))
* **studio:** bundle visual workspace and agent provisioning ([13bc447](https://github.com/Usefused/harnest/commit/13bc447d7ce86c67a7f4bdf3180cce9735e2482a))


### Fixes

* **extensions:** release packages compatible with Harnest 0.23 ([509eb41](https://github.com/Usefused/harnest/commit/509eb414402fa70b70c86e5324a95cd4ab14adec))
* **oauth:** connect Studio with temporary client credentials ([e2a8db6](https://github.com/Usefused/harnest/commit/e2a8db6d02b1eac8721bf1ed6789f639ced0fa06))

## [0.22.0](https://github.com/Usefused/harnest/compare/v0.21.3...v0.22.0) (2026-09-18)

### Features

* Enable Anthropic prompt caching by default for agents using `LiteLLMModel`:
  Harnest now marks the system instruction and final message with ephemeral
  cache breakpoints before each Anthropic request, without changing OpenAI or
  Gemini behavior. Opt out per model with `LiteLLMModel(..., prompt_cache=False)`.

* Add `MCPClient.from_openapi(...)` to connect directly to an OpenAPI spec
  through a runtime FastMCP bridge, with environment credential references
  and no CLI generation or setup step.

* Add the optional `harnest-fused` Python package to compose multiple OpenAPI
  specifications into one Fused MCP server. Select all operations by default
  or restrict each specification explicitly, provision through `fused-cli`
  during setup, and use a standard MCP client at runtime.

* Add agent-focused Studio inside the playground with graph navigation, subagent
  and capability relationships, source inspection, and local configuration and
  workflow editing through `harnest serve --reload`. Move MCP discovery into
  Studio with connection creation, removal, tool search, and tool selection.
* Add eval suite and case authoring, duplication, metric configuration, and
  reviewable Playground conversation capture. Save native eval files to the
  source workspace and activate them through validated development reload.

* Rework Studio's MCP connections tab to authenticate into Fused with OAuth
  instead of requiring a local `fused-cli` install. Connect a Fused workspace
  by dynamically registering an ephemeral public (PKCE) OAuth client from a
  per-user registration key, then list deployed MCP servers, deploy a new one
  from a pasted `kind: mcp` config, and mint a one-time execution token shown
  once in the Studio. The redirect URI is derived from the served origin;
  configure only the Engine URL and registration key through the
  `HARNEST_FUSED_ENGINE_URL` and `HARNEST_FUSED_OAUTH_REGISTRATION_KEY`
  environment variables.

* Add `harnest init --template` to download a universal `harnest-template-*`
  wheel (by PyPI project, slug, or HTTPS URL) and materialize its inert
  `template/` agent tree. The wheel is never installed, imported, or executed;
  `config.yaml` supplies the framework and mode, and `{{ .Name }}`-style
  placeholders are filled from the target directory.
* Add `harnest template package` to turn an existing agent directory into a
  universal `harnest-template-*` wheel, excluding build, cache, and environment
  state without executing the agent code.
* Let templates declare backing services under a `services:` list in
  `harnest-template.yaml`. `harnest init --template` renders them into a
  localhost-only, digest-pinned `docker-compose.yml` and injects each service's
  `provides` URLs into the scaffolded `config.yaml`; Harnest never starts the
  services or runs template code.

### Fixes

* Align Studio and Evals with the playground theme. Distinguish primary actions,
  secondary controls, navigation, and row utilities; add titled form sections,
  dividers, and separate suite setup, cases, settings, and run results. Keep all
  three workspace tabs in one compact mobile row and reduce unused header space.
* Standardize selects across Playground, Studio, Evals, and forms with shared
  themed option menus, selected checkmarks, keyboard navigation, and viewport-aware
  positioning. Apply the same option styling to the searchable session picker,
  keep its menu inside phone screens, and preserve a readable mobile trigger.
* Record the active Playground workspace and Studio view in the URL so browser
  back and forward follow tab changes, and restore the workspace from the URL
  when the playground loads.

### Changed

* Widen the official Harnest Extensions' supported Harnest range to
  `>=0.21.3,<0.24`, so the Docker, Hatchet, and RAG extensions accept the
  current release and the next minor without claiming support for pre-0.21.3
  hosts.
* Apply the same `>=0.21.3,<0.24` Harnest range to the optional `harnest-fused`
  package.

### Features

* add agent Studio and eval authoring to the playground ([4983ced](https://github.com/Usefused/harnest/commit/4983cedab88f7aad7f406c5a71888bd4e9de672c))
* **mcp:** add runtime OpenAPI clients and Fused integration ([3cdf066](https://github.com/Usefused/harnest/commit/3cdf066f89bfa8459144bde84cc31dd24e3e411d))
* **studio:** Fused OAuth-backed MCP connections ([3871638](https://github.com/Usefused/harnest/commit/38716382c05b8c79ea28cd779afcabaf173c7fe2))
* **studio:** return to MCP view after connect and offer fused-cli token command ([e9e0ff2](https://github.com/Usefused/harnest/commit/e9e0ff205d638ed4cdf94a8ece0d74d1bc218684))
* **studio:** structured MCP creation with per-operation selection ([68cc479](https://github.com/Usefused/harnest/commit/68cc4794d0a2fb16916bf1980bec4dc7d9dd5a56))

## [0.21.3](https://github.com/Usefused/harnest/compare/v0.21.2...v0.21.3) (2026-09-15)

### Fixes

* Fix the checked-in MCP agent example to use explicit OpenAI-compatible model
  configuration. Declare AnyIO directly for MCP transport and subscription deadlines.

### Fixes

* update MCP example model and declare AnyIO dependency ([1af838e](https://github.com/Usefused/harnest/commit/1af838e022412581add32715b71b474887fb3e65))

## [0.21.2](https://github.com/Usefused/harnest/compare/v0.21.1...v0.21.2) (2026-09-15)

### Fixes

* Expose MCP resource and prompt tools to models only when the server advertises
  that capability and the client allows it. Keep inspection and all governed
  developer operations available through `context.mcp` without extra model tools,
  with structured results in both ADK and LangGraph.
  Keep ADK helper schema names aligned with their registered namespaced names.

### Fixes

* **mcp:** expose only advertised model capabilities ([faea73b](https://github.com/Usefused/harnest/commit/faea73bcc6ebf5fe866299890d5623e8ffc4fcac))

## [0.21.1](https://github.com/Usefused/harnest/compare/v0.21.0...v0.21.1) (2026-09-15)

### Fixes

* Prevent MCP subscription shutdown from hanging on Python 3.10 when a
  notification or handler completes concurrently with cancellation. Preserve
  startup, idle-probe, and handler deadlines.

### Fixes

* **mcp:** preserve subscription shutdown cancellation on Python 3.10 ([d780a2e](https://github.com/Usefused/harnest/commit/d780a2eb8528016d31c9fcb22380c9a312a7a359))

## [0.21.0](https://github.com/Usefused/harnest/compare/v0.20.0...v0.21.0) (2026-09-15)

### Features

* Discover MCP resources, URI templates, and prompts through `context.mcp`,
  model-accessible retrieval tools, `harnest mcp inspect/read/prompt`, and the
  local development playground. Apply client permissions, explicit allowlists,
  pagination, and content limits without promoting remote prompts to instructions.
* Add opt-in MCP resource subscriptions with async handlers, latest-value reads
  on startup/reconnect, bounded retry, and runtime-owned shutdown. Support SDK 1.x
  resource subscriptions and explicit HTTP `subscriptions/listen` streams.

### Fixes

* Avoid A2A cancellation errors when task recovery cancels the same durable wait
  first; verify committed cancellation and handle concurrent checkpoint arming.
* Restore MCP HTTP discovery, resource reads, and subscription startup on Python
  3.10 while preserving total request deadlines and stream cleanup.
* Prefer MCP `2026-07-28` HTTP discovery for CLI inspection, agent tools,
  resources, and prompts instead of hiding capabilities behind the SDK 1.x
  compatibility handshake. Preserve initialized-server fallback without replaying tool calls.
* Allow `harnest.mcp` to be imported before `harnest.agent` in standalone code.
* Let verified PyPI Harnest Extensions synchronize successfully when their wheel
  metadata repeats Harnest or compiler-owned runtime dependencies. Materialized
  projects now retain only extension-owned requirements, and the published RAG
  package is classified as an official Fused extension.

### Features

* **mcp:** add protocol-aware discovery, resources, and subscriptions ([4e9922c](https://github.com/Usefused/harnest/commit/4e9922c58ba576bcb34bc1341e30524480adef1c))


### Fixes

* **a2a:** converge concurrent durable task cancellation ([98016bf](https://github.com/Usefused/harnest/commit/98016bf2c780d5fa835f5e6cb42780e468e54426))
* **mcp:** preserve HTTP timeout support on Python 3.10 ([1cd5ce6](https://github.com/Usefused/harnest/commit/1cd5ce6543a60f99af8678c88c20de2ecb6d3d4e))
* support published extension runtime dependencies ([d6acfc7](https://github.com/Usefused/harnest/commit/d6acfc7e44d0623ddcd60a6886ddaf436b29070c))

## [0.20.0](https://github.com/Usefused/harnest/compare/v0.19.0...v0.20.0) (2026-09-15)

### Features

* Add `harnest add mcp` for safe Streamable HTTP and SSE connection scaffolding.
  Users choose the token environment variable, destination HTTP header, and
  value prefix without passing or persisting the secret itself. Reject embedded
  URL credentials and MCP files that advanced-mode projects would ignore.

### Fixes

* Make `harnest doctor [AGENT_DIR]` detect the agent's framework and inspect its
  synchronized environment instead of assuming ADK in the shared runtime.
  Outside an agent, check core dependencies unless `--framework` is specified.
  Add discoverable response examples and schemas to OpenAPI and a curl quickstart
  to the API docs, including session reuse and streaming.
  Expose equivalent JSON/YAML OpenAPI resources through `/agent` and startup
  URLs. Let `server.openapi: false` disable both specs, documentation pages,
  and their playground/discovery links without disabling the agent API.

### Changed

* Default the agent HTTP port to `1907` in the CLI, runtime, new agent cards,
  and examples. Explicit configured ports and `--port` overrides are unchanged.

### Features

* improve agent setup and serving ([eefab47](https://github.com/Usefused/harnest/commit/eefab47d5a0f7f5e1553eb534b2d7ff423d4bb46))

## [0.19.0](https://github.com/Usefused/harnest/compare/v0.18.3...v0.19.0) (2026-09-15)

### Features

* Add the official PostgreSQL RAG Harnest Extension with typed chunk, query,
  hit, embedder, chunker, and backend contracts; atomic document replacement;
  audited mutations; tenant-scoped keyword, vector, and hybrid retrieval; and
  bounded in-memory development support. Additional datastore extensions can
  implement the same lifecycle-owned `RAGBackend` contract.

* Add explicit, async long-term memory under `harnest.memory`, `context.memory`,
  and `lifecycle.storage.memory`. Support PostgreSQL, Redis, and custom providers
  with application/user isolation, namespaces, literal text search, bounded
  pagination, revision-fenced writes, expiry, deletion, and provenance. Memory
  is opt-in: no automatic extraction, model calls, or prompt injection. Ship
  provider conformance tests and connection-configuration documentation.
  Preserve lossless Redis metadata, report uncertain write acknowledgements
  without automatic replay, and provide trusted-provider-only namespace/user
  erasure and bounded expiry cleanup. No cleanup HTTP routes or generated tools
  are exposed. Verify model-selected tools and real database crash recovery.

* Let Agent Desktop attach immutable Agent Plugin ZIP snapshots when it creates
  a managed-agent session. Restore the bounded snapshots from session storage,
  expose plugin skills and remote HTTP/SSE MCP servers in ADK and LangGraph,
  and reject URL sources, unsafe archives, mutable session changes, and dynamic
  stdio execution without a sandbox boundary.

### Fixes

* Render playground agent replies as safe Markdown, including streamed responses
  and restored sessions. Keep playground assets and the parser in the local CLI,
  outside production runtime wheels and compiled agents; deployed launchers no
  longer expose the playground by default. Local `harnest serve` retains the UI
  and honors `server.playground.enabled: false`.

* Remove the Procrastinate task/cron backend, implicit PostgreSQL fallback, and
  queue-library dependency injection. Queued tasks require an explicit
  `lifecycle.storage.tasks` provider; recurring work also requires the same
  provider under `lifecycle.storage.cron`. Report actionable setup errors and
  document shared connection configuration. Existing deployments must drain
  old work before switching storage and recompiling; old queue data is not migrated.

* Keep external continuations attached to an active WebSocket so later live
  cancellation reaches durable provider ownership. Resume attached ADK tool
  frames before returning to the model loop, and await restart-safe provider
  verification before cross-replica ADK fallback.

* Harden release publishing by requiring reviewed `main` ancestry and immutable
  action revisions, sanitize authentication-provider failures before logging,
  and keep response and session polling identifiers out of exported HTTP spans.

### Changed

* Align the independently versioned official extensions with the current
  Harnest Extension structure. Prepare Docker extension `0.4.0` and Hatchet
  extension `0.2.0` for Harnest `>=0.18,<0.23`, publish provider-specific
  documentation metadata, and remove stale Runtime Plugin terminology. Prepare
  RAG extension `0.1.1` for the same Harnest range, with its expanded PyPI guide
  and an explicit distinction
  between retrieval knowledge and Harnest long-term memory. The Harnest core
  version is unchanged.

* Rename same-process executable Runtime Plugins to Harnest Extensions across
  source layout, Python APIs, CLI discovery, runtime ownership, and compiled
  manifests. `plugins/` now unambiguously contains declarative Agent Plugins;
  executable packages use `extensions/<name>/extension.yaml`, `extension.py`,
  and `harnest.extensions`. Project schema 6 lets `harnest upgrade` preview and
  apply the directory, manifest, singleton, distribution-name, dependency, and
  authored-import migration while leaving `plugin.json` packages untouched.
  Remove the manifestless `plugins/<name>/{mcp,skills}` compatibility loader so
  every package under `plugins/` must now follow the Agent Plugins standard.
  Harnest Extensions now project tools, MCP, skills, subagents, and lifecycle
  content only from validated package-relative paths declared by
  `extension.yaml` `contributes`; installation still copies the package intact,
  and upgrade makes formerly inferred Runtime Plugin folders explicit.

* Move the human-approval API from `harnest.approval` to
  `harnest.agent.approval`, alongside the agent tools it protects. Remove the
  old root module and teach `harnest upgrade` to rewrite authored imports and
  qualified access to the new namespace.

### Features

* add dynamic agent plugins and extension packages ([0959a0b](https://github.com/Usefused/harnest/commit/0959a0bb2e104445be01e32c6bfeac149581bbb3))
* add explicit durable memory with scoped provider cleanup ([a4304cc](https://github.com/Usefused/harnest/commit/a4304cc6343c1a87cc0768f2a0c31f1812a8133d))
* add PostgreSQL RAG extension ([aff613c](https://github.com/Usefused/harnest/commit/aff613c53e101fb8e6f750e2ba48f08a30427017))


### Fixes

* harden live continuations and release safety ([f41e6cf](https://github.com/Usefused/harnest/commit/f41e6cf99a2ea90b63826f2b71f74c50a81efd9a))
* render playground Markdown and exclude UI from deployments ([2be254f](https://github.com/Usefused/harnest/commit/2be254f980e593c2ef0a4861c3d189ff08988c05))
* support PostgreSQL memory locks on Python 3.10 ([7eaee7d](https://github.com/Usefused/harnest/commit/7eaee7d338321e6ad96c749ff09dfeda4961e3c8))
* widen official extension Harnest compatibility ([4239e05](https://github.com/Usefused/harnest/commit/4239e052e61b385d34cca9b9933d33124a508972))

## [0.18.3](https://github.com/Usefused/harnest/compare/v0.18.2...v0.18.3) (2026-09-11)

### Fixes

* **Models:** Fix provider-specific defaults by using `LiteLLMModel.from_openai_environment()` and a generic OpenAI-compatible API contract. Configure `OPENAI_MODEL` and `OPENAI_BASE_URL` explicitly; Harnest no longer selects a vendor endpoint or GPT model. New agents, examples, judges, and simulators share this configuration, with optional `OPENAI_API_KEY` authentication. Remove `OllamaModel`; existing agents must migrate their imports and endpoint settings.

### Fixes

* use generic OpenAI-compatible model configuration ([a2a8be0](https://github.com/Usefused/harnest/commit/a2a8be04fdbe21071a21d29f1a1f3044c000892a))

## [0.18.2](https://github.com/Usefused/harnest/compare/v0.18.1...v0.18.2) (2026-09-09)

### Fixes

* Exclude provider reasoning and intermediate response chunks from ADK judge
  verdict parsing, including rubric evaluations for ADK and LangGraph agents.
  Preserve the final answer, provider configuration, and authored model output.

### Fixes

* parse only final judge responses in evaluations ([10205eb](https://github.com/Usefused/harnest/commit/10205eb69b3b1500f0247e14e69aa49d1c7eb8fb))

## [0.18.1](https://github.com/Usefused/harnest/compare/v0.18.0...v0.18.1) (2026-09-09)

### Fixes

* Make model context and token budgets controllable through opt-in per-agent
  policies for managed ADK and LangGraph agents. Support observation, explicit
  limits, configurable history and tool-text reductions, custom strategies,
  and trusted invocation overrides while preserving stored transcripts.

### Fixes

* make token utilisation configurable and opt-in ([c839c54](https://github.com/Usefused/harnest/commit/c839c543f64882f7731958babee8e46e309dbc3f))

## [0.18.0](https://github.com/Usefused/harnest/compare/v0.17.0...v0.18.0) (2026-09-09)

### Features

* Default newly scaffolded agents, SubAgents, the helpdesk example, and implicit
  evaluation judges/simulators to Ollama instead of OpenAI. Add
  `OllamaModel.from_environment()` with `OLLAMA_MODEL`, `OLLAMA_BASE_URL`, and
  optional `OLLAMA_API_KEY`; retain explicit OpenAI configurations. The default
  `qwen3.5:cloud` uses Ollama's cloud service through its local daemon. Preserve
  configured Ollama transports across framework and evaluation adapters.

* Add database-neutral durable Task and cron storage contracts, selected through
  `lifecycle.storage.tasks` and `lifecycle.storage.cron`. Harnest owns execution,
  retries, lease renewal and continuation recovery; providers atomically claim
  work and commit scheduled occurrences. Bundle the `harnest_postgres` and
  `harnest_redis` import packages in Harnest's matching runtime wheel, with no
  separate provider installation or PyPI dependency. Keep database drivers in
  optional/framework extras. Include an in-memory test provider and reusable
  conformance tests for custom databases. Explicit providers do not load
  Procrastinate; existing applications retain the legacy backend until they
  explicitly switch after draining their existing work.

### Fixes

* Create owner-scoped cron occurrence sessions before task execution acquires
  their leases, so scheduled tasks run with PostgreSQL, Redis, or custom session
  stores after restart without resetting session state on retries.

* With explicit Task storage, reconcile removed and retargeted static schedules,
  preserve cancellation across recovery, and isolate worker startup from the
  caller's invocation permissions.

### Features

* add portable durable task and cron storage ([69af6b9](https://github.com/Usefused/harnest/commit/69af6b9daa40be9eea2e17d36a95cfa0d4eeb0dd))
* bundle database providers with the Harnest runtime ([397334c](https://github.com/Usefused/harnest/commit/397334ccd3bc38a100ca8ae3b4c5407391a23168))
* default agents and evaluations to Ollama ([22367ac](https://github.com/Usefused/harnest/commit/22367ac0023a746da9a82b7f595f05deb16ac284))

## [0.17.0](https://github.com/Usefused/harnest/compare/v0.16.0...v0.17.0) (2026-09-08)

### Features

* Add durable, user-owned recurring Task schedules through `harnest.cron`.
  Managed runtime code can create, get, list, update, pause, resume, cancel, and
  delete schedules that target compiled Tasks. Schedule access is scoped to the
  active user, uses idempotent caller keys, and runs on the existing UTC Task
  backend in long-lived serving processes.

### Features

* add user-owned dynamic cron jobs ([72a5cd2](https://github.com/Usefused/harnest/commit/72a5cd21c641ea2ec1c38a5c124d086e74532191))
* organize authoring APIs by namespace ([fd77242](https://github.com/Usefused/harnest/commit/fd77242d93a03b91131013f5b21aff77a7a76158))

## [0.16.0](https://github.com/Usefused/harnest/compare/v0.15.0...v0.16.0) (2026-09-07)

### Fixes

* Keep direct `harnest compile` independent from pytest so the command runs in
  the intentionally lean production dependency profile.

### Changed

* Add an explicit `harnest init --minimal` profile that emits only runnable core
  files. Build it up safely with `harnest add tool`, `subagent`, `task`,
  `lifecycle`, or `context`; additive scaffolds recreate optional folders,
  validate framework ownership, and never overwrite existing resources.
  `harnest test` now treats an absent optional test folder like an empty suite.

* Make Harnest features explicit public namespaces instead of flattening their
  classes, decorators, and functions onto `harnest`. Agent and tool contracts
  now live together under `harnest.agent`; there is no `harnest.tool` public
  module. Make `harnest.lifecycle` and `harnest.context` first-class module
  namespaces. Lifecycle hooks now use grouped paths such as
  `lifecycle.storage.sessions` and `lifecycle.agent.before`, while context
  providers use `@context.provider(...)`. Remove the same-named facade objects
  and flat lifecycle aliases; `harnest upgrade --apply` splits existing root
  imports across their owning domains, moves old tool imports to
  `harnest.agent`, and rewrites decorator paths with a backup.

* Make every generated folder `_README.md` state whether the folder is
  optional, explain when it can be deleted, and include a minimal example of
  the code or configuration the folder owns. Advanced-mode guides also call
  out folders that Harnest does not discover.

* Prepare Docker extension `0.3.0` with bounded multi-container topologies,
  extension-owned bridge networks, internal DNS aliases, service readiness,
  per-container budgets, reverse-order cleanup, and privacy-safe lifecycle logs
  and traces carrying `harnest.extension.name=docker`. Replace raw
  retention-scope strings with `DockerScope`. Prepare Hatchet extension `0.1.1`;
  both packages target Harnest `>=0.15,<0.16`.

### Features

* add Docker service topologies ([2e01dae](https://github.com/Usefused/harnest/commit/2e01dae63bde133cd5e2470167ba69b42da016bd))


### Fixes

* keep compile independent from pytest ([ab521ac](https://github.com/Usefused/harnest/commit/ab521ac702d4a8018cee8fd222d9ac4c2950b04b))

## [0.15.0](https://github.com/Usefused/harnest/compare/v0.14.0...v0.15.0) (2026-09-07)

### Features

* Cache validated `harnest serve` artifacts by authored bundle and managed
  compiler identity. Reuse unchanged generations without importing the agent
  graph again, and remove superseded compiled generations asynchronously only
  after a replacement is published successfully. Lease managed environments
  across `compile`, `test`, `run`, and `serve`, and asynchronously reclaim old
  unleased dependency fingerprints instead of accumulating them indefinitely.
  Keep the managed Python compiler warm during `harnest serve --reload` so
  source changes reuse imported Harnest and framework dependencies while still
  producing and validating a fresh immutable generation. Split the lean
  production runtime, shared development, and eval dependency profiles into
  independent environments and locks; reserve the runtime profile for
  `compile`, let `serve`, `run`, and ordinary tests share development tooling,
  omit Google ADK's Kubernetes-backed extension bundle, and install MCP
  adapters only for authored MCP clients.

* Let `OutputPolicy` independently suppress public tool activity, provider
  thinking, and model/provider metadata across every neutral transport.
  Thinking remains private by default; tool activity and normalized metadata
  remain visible unless explicitly suppressed. `OutputPolicy` is now
  keyword-only and requires booleans for binary disclosure controls plus
  `AgentMetadataMode` for normalized-versus-raw metadata; the former string
  policy values are no longer accepted. `harnest upgrade --apply` migrates
  released string and positional forms to the strict contract.

* Add `server.agentPrincipal: required` so security-sensitive deployments can
  reject custom `AgentInvoker` routes that omit a principal before any session
  or invocation state is created.

* Add Hatchet `run_and_wait(...)`, which verifies durable continuation support
  before submitting an external run. Persist versioned private permission-name
  snapshots for external continuations in memory, PostgreSQL, and Redis, then
  reconstruct fresh Agent Runtime Principals when another replica resumes the
  wait without persisting opaque principal identity or credentials.

### Fixes

* Accept only universal `py3-none-any` PyPI wheels for Harnest Extensions so a
  platform- or ABI-specific artifact cannot be selected for the wrong host.

### Features

* harden runtime and development workflows ([0e1ddd1](https://github.com/Usefused/harnest/commit/0e1ddd1e6e58050a066bc09bfd6608c2af2a9c05))
* migrate output policies on project upgrade ([bfa1da2](https://github.com/Usefused/harnest/commit/bfa1da253cfb0ab5247d788cbccc1180baff6fd8))


### Fixes

* install extension build backend in CI ([c06ea06](https://github.com/Usefused/harnest/commit/c06ea06d7240173ee9e2f4fb39685ec1fd314ba9))
* isolate production compile dependencies ([fa9e815](https://github.com/Usefused/harnest/commit/fa9e815032d4fc38e5f76ff3a7ea50397946e65f))


### Refactoring

* remove stale test profile compatibility ([36ed82e](https://github.com/Usefused/harnest/commit/36ed82e5dc7e944d75c5a3a9b720bbd7b33dea3e))

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
