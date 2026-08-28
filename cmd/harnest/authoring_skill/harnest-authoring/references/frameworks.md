# Framework and mode decisions

Read this reference when changing framework, graph topology, subagents, model
integration, sandboxing, or advanced framework-native behavior.

## Managed mode

Managed mode is the default. Author an `Agent` or portable `Graph`; Harnest
discovers filesystem resources, composes them, and lowers the result to the
framework selected in `config.yaml`.

- ADK supports portable graphs plus ADK-specific agent fields, native plugins,
  progressive ADK skills, and sandbox executors.
- LangGraph supports the same portable graph topology and portable skills.
  MCP discovery is deferred until runtime, including MCP-enabled agent nodes
  inside graphs.
- Prefer explicit graph nodes for multi-agent flows. Flat `subagents/` are
  discovered capabilities, but LangGraph does not implicitly attach ADK-style
  child agents to one `Agent` definition.
- An inline `Agent` node in the root `agent.py` is root-scoped. A folder-based
  `subagents/<name>/agent.py` is separately composed from its own sibling
  resources and does not inherit root tools or skills.
- ADK folder-based agents may recursively discover child subagents. A nested
  LangGraph `Agent` definition cannot consume a discovered sibling
  `subagents/` folder today; model the flow explicitly in the root graph.
- ADK-specific `sandbox`, `output_key`, and `generate_content_config` settings
  are not portable to LangGraph.

Use a portable graph when the same authored topology should work across both
frameworks. Use callable nodes and `Event(route=...)` for deterministic routing;
use `Join()` to synchronize parallel branches.

## Advanced mode

Advanced mode is an escape hatch for framework features that cannot be expressed
by the portable IR. `agent.py` exports `Agent.advanced(target=...)` as the
configured entrypoint. The author imports ADK or LangGraph directly and owns
framework construction, lifecycle wiring, state semantics, and provider-specific
dependencies; Harnest does not wrap or re-export either framework.

Do not switch a project to advanced mode merely to add a tool, MCP client,
progressive skill, portable graph, or lifecycle extension; managed mode already
supports those conventions.

Never regenerate or overwrite an existing `agent.py` to switch modes. Audit an
edited project first:

```bash
harnest mode advanced AGENT_DIR --check
```

This command is read-only. It reports the current framework, mode, entrypoint,
managed folders that need explicit wiring, and the responsibility boundary. In
advanced mode Harnest keeps the neutral server, authentication, sessions,
approvals, tracing, and portable invocation extensions. The agent owns native
routing, state, checkpoints, middleware/plugins, capability declaration,
framework upgrades, and arbitrary native model calls. Explicitly decorated
native capabilities inherit the neutral invocation context; opaque or direct
native execution needs framework wiring. Portable model hooks are guaranteed only at Harnest-managed/wrapped
model boundaries. The check never updates files; migrate semantically and
preserve unrelated changes.

## Framework changes

Changing `spec.framework.name` is an architectural migration, not a spelling
edit. Before changing it:

1. Inspect agent fields, native extensions, sandbox use, evals, and custom graph
   nodes for framework-specific behavior.
2. Update provider dependencies in `pyproject.toml`; never add either framework.
3. Replace unsupported fields or move truly native logic behind the selected
   framework integration.
4. Run unit tests, compilation, and explicitly authorized smoke/eval lanes.

ADK eval execution is root-scoped: `harnest test --evals` does not select eval
files below nested agent folders.

## Framework versions

ADK and LangGraph compatibility is release-bound. Each installed Harnest
release declares bounded supported ranges for both frameworks and the compiler
checks the selected installed distribution before loading agent code. The same
check applies to managed and advanced mode. Direct imports in
`Agent.advanced(...)` provide framework-native authoring control; they do not
bypass Harnest's tested runtime boundary.

Never declare Harnest, ADK, LangGraph, or Harnest-owned framework adapters in
the agent's `pyproject.toml`; environment synchronization rejects them. If a
requested framework version is outside the release range, upgrade Harnest and
test the agent. A compiled artifact records both the Harnest version and exact
selected framework version for deployment diagnostics.

## Conversation history

Use `Agent(..., history="session")` for native multi-turn behavior; it is the
default for root agents, subagents, and graph agent nodes. The model receives
earlier user/assistant turns only from the same Harnest session. Use
`history="turn"` when deterministic isolation matters more than conversation
continuity. Do not carry a duplicate transcript in graph state. The required
checkpoint lifecycle persists in-progress execution; Harnest session storage
remains the committed conversation authority.

## Models and Ollama

Use a LiteLLM-qualified name such as `ollama_chat/qwen3.5:cloud`. Ollama's native
API base is commonly `http://127.0.0.1:11434` locally or `https://ollama.com`
for Ollama Cloud; an OpenAI-compatible `/v1` base is not the native Ollama
`/api/chat` base used by the `ollama_chat` provider. Credentials remain runtime
environment or secret configuration.

Both managed frameworks use the same model mode contract. `thinking=True`
requests reasoning, `thinking=False` requests no reasoning, and omission uses
the provider default. LiteLLM maps the non-thinking mode to Ollama's
`think: false`. Use `reasoning_effort` directly when a specific supported level
is required. Harnest filters native ADK thought parts and LangGraph thinking
blocks at its public boundary while leaving them available to the framework for
multi-turn continuity.

For a team model gateway, pass a `LiteLLMLifecycle` to `LiteLLMModel`. Use
`create_transport` to return a LiteLLM-supported provider SDK client configured
with custom HTTP or mTLS behavior. Use `before_request`, `after_response`, and
`on_error` for per-call routing, headers, normalization, and diagnostics; close
owned resources in `close`. Hooks may be ordinary or async methods, but async
hooks require async execution and one built model cannot mix call modes. The
lifecycle is per model adapter and never patches LiteLLM globals.
