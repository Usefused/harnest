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
  Neither path starts provider infrastructure or calls a provider factory during
  compilation.
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
  metadata, and the optional `SandboxNetworkPolicy`. Identity can be absent
  outside a Harnest invocation. Providers requiring identity must reject such
  requests rather than invent a tenant.
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

## Provider-owned network policy

`Sandbox.provider(..., network_policy=...)` makes Harnest the source of network
authority while leaving enforcement below agent code. The immutable policy has
three modes: `none`, `unrestricted`, and an exact host `allowlist`; allowlists
can additionally restrict destination ports and require private-network
blocking. Exact hosts deliberately exclude URL, wildcard, path, and embedded
port syntax. Providers must reapply hostname and resolved-address decisions to
every connection, including redirects and repeated DNS resolutions.

A portable provider accepting this policy exposes `sandbox_capabilities` as a
`SandboxProviderCapabilities` value. Harnest validates the requested mode, host
and port filtering, and private-network blocking before the first execution.
Missing or partial guarantees raise `SandboxPolicyUnsupportedError`; application
checks are never counted as provider enforcement. Omitting `network_policy`
retains the existing provider-owned contract for compatibility. Native ADK
executors cannot accept Harnest network policy because they have no capability
declaration boundary.

Reusable Harnest Extensions that implement a provider declare the closed
`sandbox.provider` extension capability. This grants trusted same-process
provider integration; it does not weaken the runtime capability validation.

## Provider execution boundary

Core Harnest does not ship a container daemon client or a built-in container
factory. `Sandbox.provider()` is the provider-neutral declaration boundary, and
Harnest Extensions own their SDK dependency, configuration helper, isolation
mechanism, and provider-specific conformance tests. Extensions return the same
`SandboxRequest` / `SandboxResult` contract to both frameworks.

- `SandboxBudget` is a portable resource request. A provider must translate it
  into controls it genuinely enforces and fail when it cannot satisfy the
  requested ceiling; core does not translate budgets into SDK-specific options.
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
  Python. Provider cleanup should use this scope and bounded transport calls;
  failed cleanup must retain ownership for retry.
- Results include explicit `SandboxStatus` and optional `exit_code`. Successful
  stderr warnings remain successful; nonzero process exits fail even without
  stderr. Timeouts and output overflow have distinct statuses. Provider SDK
  failures remain sanitized exceptions with a status field.
- Providers own file validation and output artifacts. An extension that cannot
  accept input files raises the public `SandboxInputFilesUnsupportedError`, which
  the runtime converts to a stable sanitized execution error.
- `adapter_options` are retained only by the native adapter. They let an
  extension configure supported ADK parsing or retry fields without using
  Harnest's private dataclass fields; named grants do not expose them to models.

## Custom provider responsibility

Provider backends own filesystem and tenant isolation, CPU/memory limits,
execution termination, bounded SDK output, network and credential policy,
concurrency, and cleanup. Harnest rejects revoked managed contexts instead of
silently using anonymous identity and checks admission before provider calls;
it cannot interrupt an arbitrary third-party SDK once that SDK starts running.
Harnest cannot apply a portable resource budget to arbitrary providers.
`config.yaml` permissions describe deployment intent and cannot replace an
enforcing sandbox implementation.
