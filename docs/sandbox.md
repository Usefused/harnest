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
  The built-in backend still delegates Python execution to the native ADK
  `ContainerCodeExecutor`.
- Unassigned lookup raises `ContextResourceError`. Access outside a managed
  invocation, or reuse of a handle in another or revoked invocation, raises
  `ContextUnavailableError`. Handles do not transfer authority between agents.
  Provider failures become sanitized `SandboxExecutionError`; returned stderr
  remains part of `SandboxResult` and must be handled by the authored tool.
- Explicit legacy `Agent(sandbox=...)` remains supported but is mutually
  exclusive with `sandboxes`. It lowers to ADK's native `code_executor` or
  LangGraph's `harnest_execute_python` tool. `sandbox/sandbox.py` now declares
  the name `sandbox` and requires explicit `sandboxes=["sandbox"]` assignment.
- Legacy ADK sandbox agents wrap only their own native code-response processor. When
  code execution emits a result and consumes a `STOP` response, the discarded
  response's finish reason is cleared so ADK 2.8's empty-response guard does not
  terminate the next model turn. Native events, errors, retry exhaustion, and
  genuinely empty final responses remain unchanged; no shared ADK code is patched.
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

The built-in provider shares ADK's native Python execution across both
frameworks. Harnest owns the compatible `google-adk[extensions]` dependency.
The actual ADK `ContainerCodeExecutor` owns the execution command and container
configuration. A per-instance host-side transport guard enforces deadlines,
cancellation, bounded output, and cleanup without globally patching ADK or Docker.

- Exactly one `image` or `docker_path` is required. Docker is mandatory at
  execution time; no host-process fallback exists.
- The native executor is created lazily and reused between executions. Its
  writable filesystem may persist and be shared across users and sessions.
- Application shutdown closes only constructed providers via `close()` or
  async `aclose()`. The built-in backend removes its owned container; provider
  wrappers must forward cleanup to the backends they own.
- Defaults are networking off and a 300-second timeout. Harnest does not add
  per-session filesystem isolation or CPU/memory limits here. It does not
  override native Docker startup with additional resource or process caps.
- Successful calls stop all container processes, then restart the same native
  container on the next call. This prevents detached children from outliving
  execution without separating or clearing the writable filesystem.
  Deadline violations, output overflow, and active cancellation force removal
  of that container, so its
  files are lost. The next call creates a new native executor only after
  removal is confirmed. Failed startup also cleans up any acquired container.
- `max_output_bytes` is a positive combined stdout/stderr byte budget, default
  1,048,576. The guard bounds raw Docker frame reads before accumulating output;
  truncating after the native SDK has buffered everything is not sufficient.
  A short fixed guard diagnostic may be added after program output is discarded.
- The host-side deadline cannot be suspended by code signaling its in-container
  supervisor. Queue waiting counts toward the absolute deadline. Initial image
  preparation and Docker control-plane startup use SDK transport timeouts;
  admission is rechecked when those operations return, not forcibly interrupted.
  Cancelled, expired, or revoked calls cannot begin after a prior call releases
  the lock.
- Cleanup failure retains a poisoned executor; subsequent calls must confirm
  removal before constructing a replacement. Docker daemon availability is
  required to confirm termination; an async timeout alone is not termination.
  An ambiguous create failure without a returned container ID blocks automatic
  retry and requires operator reconciliation of the Docker daemon's resources.
- Native options are allowlisted: `error_retry_attempts`,
  `code_block_delimiters`, `execution_result_delimiters`, `stateful`, and
  `optimize_data_file`. The last two must remain false. Options cannot replace
  explicitly declared container settings. Code-block delimiters and executor
  retry options apply only to the legacy ADK code-executor loop. Authored tools
  own result interpretation and retry decisions for named capabilities.
- The built-in provider rejects input files instead of silently ignoring them.
  It returns native stdout/stderr; it does not provide durable session files or
  an output-file collection service.

Docker execution is not a claim of tenant separation, bounded CPU/memory, or
VM-grade isolation from a hostile shared kernel. Deployment and provider
configuration must enforce any additional isolation and resource requirements.

## Custom provider responsibility

Custom backends own filesystem and tenant isolation, CPU/memory limits,
execution termination, bounded SDK output, network and credential policy,
concurrency, and cleanup. Harnest rejects revoked managed contexts instead of
silently using anonymous identity and checks admission before provider calls;
it cannot interrupt an arbitrary third-party SDK once that SDK starts running.
Harnest does not add filesystem separation or CPU/memory budgets to any provider.
`config.yaml` permissions describe deployment intent and cannot replace an
enforcing sandbox implementation.
