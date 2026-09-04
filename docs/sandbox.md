# Sandbox implementation contract

Public configuration and provider examples live in the canonical
[Sandboxing documentation](https://docs.usefused.com/harnest/build/sandboxing).
This reference defines the compiler, adapter, and isolation contracts covered
by repository tests.

## Authoring and framework boundaries

- Sandboxes are optional, explicitly assigned execution capabilities called by
  authored tools. Only the submitted code executes in the sandbox; the tool's
  surrounding Python and the agent server are not isolated by this declaration.
- `sandbox/<name>.py` exports a matching `Sandbox` variable. Names are 1–47 ASCII
  identifier characters. Underscore-prefixed examples remain ignored. Discovery
  rejects symlinks, invalid names or exports, and ancestor-name duplicates.
- Each agent explicitly assigns `sandboxes=["calculations", "research"]`.
  Root catalog names are visible to all same-project agents, including flat,
  nested, graph, and code-defined subagents. Child catalogs can add local names.
  Catalog visibility is not permission: assignments never inherit, and a
  populated folder without assignments grants no sandbox access.
- Named assignments never create model tools. A business `@tool` obtains
  `context.sandboxes["<name>"]` during a managed invocation and calls
  `execute(code, *, input_files=())` or async `aexecute` to receive a
  `SandboxResult`. Its author chooses code, input validation, and which result
  fields to return to the model. Both frameworks retain their native tool loop.
  Neither path starts Docker or calls a provider factory during compilation.
  The built-in backend executes through the framework-independent Docker provider.
- Unassigned lookup raises `ContextResourceError`. Access outside a managed
  invocation, or reuse of a handle in another or revoked invocation, raises
  `ContextUnavailableError`. Handles do not transfer authority between agents.
  Provider failures become sanitized `SandboxExecutionError`; returned stderr
  remains part of `SandboxResult` and must be handled by the authored tool.
- `Agent(sandbox=...)` is removed. Use named grants and authored tools for one
  or many sandboxes. Direct native-adapter helpers are separate embedding APIs.
- Portable provider factories return `SandboxBackend` implementations with
  `execute(SandboxRequest) -> SandboxResult`. Legacy ADK `BaseCodeExecutor`
  factories remain compatible only with ADK; LangGraph rejects them explicitly.
- Requests carry code, deadline, invocation identity, input files, execution ID,
  and metadata. Identity can be absent outside a Harnest invocation. Providers
  requiring identity must reject such requests rather than invent a tenant.
- `SandboxFile` carries bytes or base64 text, never host filesystem access.
  Provider implementations own file-path validation and artifact handling.

## Provider properties and metadata

Provider SDK configuration belongs in the lazy factory. Declaration metadata
is a separate JSON object forwarded into portable requests; it is not a
model-editable tool argument. Metadata on declarations, requests, and results
is deeply frozen and omitted from their representations. Validation rejects
non-string object keys, unsupported SDK objects, nonfinite numbers, and cycles.
Transport conversion preserves JSON scalar types and returns fresh dictionaries
and lists.

Named capabilities preserve provider metadata in `SandboxResult`; authored tools
choose which values to expose in their model-visible return value. Avoid
returning secrets, private identifiers, or the complete provider response.
Legacy ADK code execution has no native metadata field: nonempty result metadata wraps stdout in a JSON
envelope with `stdout` and `metadata` keys. Empty metadata preserves the original
stdout. Stderr retains ADK's native error behavior. Providers must not return
secrets or private identifiers as result metadata.

## Built-in container boundary

`Sandbox.container()` uses Harnest's Docker provider. It never imports ADK or
LangGraph to execute code. Both agent frameworks call it through the same
`SandboxRequest` / `SandboxResult` contract.

- Default `scope="execution"` owns a fresh container and removes it after every
  execution. `invocation` and `session` scopes key reuse by agent/user/session,
  with invocation ID additionally required for invocation scope. Missing
  identities fail before provider startup. An LRU cap (`max_scopes=8`) bounds
  retained containers per grant; eviction requires confirmed cleanup.
- Reuse does not provide durable files. The read-only root and tmpfs `/tmp`
  prohibit persistent writes. Stopping the container after every call kills
  detached children and clears scratch. New or different identities never
  obtain a previous identity's container. Application shutdown closes all
  initialized scopes and keeps failed cleanup handles available for retry.
- `SandboxBudget` defaults: one CPU, 512MiB memory with no extra swap, 64 PIDs,
  and 64MiB scratch. Docker creation enforces limits before any Python probe.
  Containers use UID/GID 65534, drop all capabilities, deny privilege escalation,
  and have no network unless explicitly enabled. Images declaring Docker volumes
  are rejected because implicit volumes would bypass the scratch budget.
- The host guard enforces deadlines including queue admission, raw streaming
  stdout/stderr bounds, cancellation, and removal. It cannot be disabled by
  signals within executed code. `timeout_seconds=300` and 1MiB combined output
  are defaults. Docker control-plane operations retain finite SDK I/O timeouts;
  admission is rechecked after they return. Docker availability is needed to
  confirm actual cleanup; ambiguous creation without an ID blocks retry.
- Nested execution controls own local deadlines capped by every ancestor's
  absolute deadline. Returning from a shorter helper does not shrink or restart
  the outer budget. Copied worker contexts retain their helper's limit, while
  explicit cancellation and captured managed-context revocation remain call-wide.
  Ordinary exceptions and local deadline expiry revoke the failed scope and its
  descendants, leaving a caller that catches the error and healthy siblings usable.
- Public provider controls use `from harnest.sandbox import control`:
  `control.execute(...)` opens an execution scope, `control.current()` returns
  its active token, and `control.cleanup(...)` opens a cleanup-only scope.
  `sandbox_control.py` remains an implementation module; existing function imports
  remain compatible.
- `control.cleanup(timeout_seconds=5)` temporarily admits resource release under
  a separate finite deadline after execution cancellation or context revocation.
  Nested cleanup cannot extend that deadline; exiting revokes retained cleanup
  contexts. Execution controls reject cleanup contexts before provider admission.
  Cleanup is cooperative: check admission before each operation and pass
  `remaining()` to SDK transport timeouts. It cannot stop arbitrary blocking
  Python. Built-in Docker removal uses this scope and a bounded transport call;
  failed removal retains ownership for retry.
- Results include explicit `SandboxStatus` and optional `exit_code`. Successful
  stderr warnings remain successful; nonzero process exits fail even without
  stderr. Timeouts and output overflow have distinct statuses. Provider SDK
  failures remain sanitized exceptions with a status field.
- Container file transfer is unsupported and rejected before startup. Custom
  providers own file validation and output artifacts. `options` parsing fields
  are retained for direct `to_adk_executor()` embedding only, not named grants.

Real Docker integration tests exercise resource settings, non-root/read-only
execution, cleared scratch, timeouts, output overflow, and explicit exit status.
The compatibility CI runs these alongside both frameworks' named-grant and
shared evaluation tests. Docker is a shared-kernel boundary, not VM isolation.

## Custom provider responsibility

Custom backends own filesystem and tenant isolation, CPU/memory limits,
execution termination, bounded SDK output, network and credential policy,
concurrency, and cleanup. Harnest rejects revoked managed contexts instead of
silently using anonymous identity and checks admission before provider calls;
it cannot interrupt an arbitrary third-party SDK once that SDK starts running.
Harnest cannot apply the built-in Docker budgets to arbitrary third-party providers.
`config.yaml` permissions describe deployment intent and cannot replace an
enforcing sandbox implementation.
