# Harnest project layout

Read this reference when adding, moving, or removing authored resources.
Use [folder-edits.md](folder-edits.md) for the safe modification workflow; this
file is the path and ownership contract it relies on.

## Required root files

| Path | Purpose |
| --- | --- |
| `config.yaml` | Deployment resources, runtime environment, entrypoint, framework, and mode. |
| `server.yaml` | Standalone server binding, request limits, and playground policy. Setting values may be exact `${NAME}` environment references; never put secrets, auth, storage, or TLS here. |
| `agent-card.yaml` | Public agent identity, interfaces, capabilities, and advertised A2A skills. |
| `agent.py` | Exports a managed `Agent`/portable `Graph`, or an `Agent` created with `Agent.advanced(...)`. |
| `instructions.md` | Non-empty root instructions. Managed `Agent` definitions may omit `instruction`; the compiler supplies this file. |
| `pyproject.toml` | Agent and provider dependencies synchronized by Harnest. Never add Harnest, ADK, LangGraph, or Harnest-owned framework adapters. |
| `uv.lock` | Resolved dependency lock created by `harnest env sync`; commit it after review. |

The usual entrypoint is `agent:root_agent`. Authored source is not a Python
package and does not need `__init__.py`.

## Reusable library

| Path | Contract |
| --- | --- |
| root `lib/**/*.py` | Ordinary reusable Python imported below `harnest.lib`; never discovered as a tool, agent, MCP client, plugin, extension, or skill. |

`lib/` is root-only and global to the compiled bundle in managed and advanced
mode. `lib/audit.py` imports as `harnest.lib.audit`, while
`lib/storage/queries.py` imports as `harnest.lib.storage.queries`. Namespace
packages need no `__init__.py`; do not import helpers as bare `lib.*`. Add an
initializer only when the library itself needs initialization. Entry points and
resources at any ownership depth may use the library.
The same imports work during compilation, tests, evals, and standalone serving.

## Discovered resource folders

| Path | Contract |
| --- | --- |
| `tools/<name>.py` | Exports one `@tool`-decorated callable named `<name>`. |
| `subagents/<name>.py` | Exports one `AgentDefinition` named `<name>` with an explicit instruction. |
| `subagents/<name>/agent.py` | Recursively composed subagent named `<name>` with its own folder-scoped `instructions.md`, tools, MCP clients, sandbox, and skills. ADK also permits child subagents; LangGraph does not. Plugins and extensions remain root-only. |
| `mcp/<name>.py` | Exports one literally zero-parameter `client()` factory returning `MCPClient`; `<name>` is its local identity. |
| `plugins/<plugin>/mcp/<name>.py` | Plugin-owned `client()` factory using the same rule. |
| `plugins/<plugin>/skills/<skill>/SKILL.md` | One progressive skill teaching the host agent when and how to use the plugin's MCP tools. |
| `extensions/sessions.py` | Required root `@lifecycle.session_store` zero-argument factory returning an application-owned `SessionStore`. Exactly one factory must exist. |
| `extensions/**/*.py` | Other arbitrary public root modules; only `@lifecycle.*` functions are discovered. Multiple listeners may share an invocation phase. |
| `sandbox/sandbox.py` | Exports one `Sandbox` as `sandbox`; managed ADK only. |
| `skills/<skill>/SKILL.md` | Progressive internal instructions. Frontmatter `name` matches `<skill>`; references, assets, and scripts may live below it. |
| root `evals/<id>.evalset.json` | ADK `EvalSet` whose ID matches the filename. Optional root `evals/test_config.json` configures evaluation. |
| `tests/unit/test_*.py` | Offline authored tests. |
| `tests/smoke/test_*.py` | Explicitly enabled live model, MCP, or HTTP tests. |

Plugins contain only MCP clients and skills. A non-empty plugin must have at
least one MCP module and one skill. Plugins never contain agents or lifecycle
behavior. Use `subagents/` for agents and `extensions/` for lifecycle behavior.

## Agent ownership scopes

- Each folder-based `agent.py` owns the supported resource folders beside it.
  Parent tools and skills do not leak into nested folder-based agents.
- A nested ADK agent may discover child agents in its sibling `subagents/`.
  Nested LangGraph `Agent` definitions cannot consume discovered child agents.
- A flat `subagents/<name>.py` cannot own a private `instructions.md`, `tools/`,
  `skills/`, or other resource folder. Promote it to
  `subagents/<name>/agent.py` when private resources are needed.
- An inline `Agent` graph node defined in the root `agent.py` is root-scoped and
  uses the root folder's discovered resources.
- Plugins and extensions are root-only; nested instances fail compilation.
- `lib/` is also root-only, but its modules are globally importable throughout
  the bundle rather than attached to an agent's discovered resource scope.
- Do not add `Agent` tool/skill name lists as access selectors. Location grants
  scope. There is no separate `SubAgent` class; nested definitions use `Agent`.
- Keep executable eval assets at the root. Nested eval files may be validated
  during compilation but are not run by `harnest test --evals`.

## Discovery invariants

- Missing, empty, ignored-only folders are skipped.
- Default `harnest init` fills optional folders with ignored `_README.md`
  guides; `--example`
  is the explicit working-sample scaffold.
- Once a public resource exists, the full convention is strict.
- Resource discovery is deterministic by path name.
- Duplicate tool, MCP configuration, subagent, or skill identities fail. MCP
  configuration equality ignores compiler identity and approval metadata.
- MCP capabilities receive deterministic path-scoped runtime identities, so
  same-named direct, plugin, and subagent clients remain distinct.
- Library modules are copied and importable, but their callables are never
  discovered or injected into an agent.
- Public symlinks are rejected so compiled artifacts remain self-contained.
- Compilation validates `server.yaml` and copies a mutable operational copy
  beside `harnest-agent`; the authored copy remains under `source/`. Exact
  `${NAME}` values are preserved and resolved only when the launcher starts.
- Do not edit or commit `.harnest/`; it is disposable compiler output.
- Runtime `skills/` are not the same as `.agents/skills/harnest-authoring/`,
  which teaches a coding agent how to modify this project.
