# Harnest project layout

Read this reference when adding, moving, or removing authored resources.

## Required root files

| Path | Purpose |
| --- | --- |
| `config.yaml` | Deployment resources, runtime environment, entrypoint, framework, and mode. |
| `agent-card.yaml` | Public agent identity, interfaces, capabilities, and advertised A2A skills. |
| `agent.py` | Exports a managed `Agent`/portable `Graph`, or an `Agent` created with `Agent.advanced(...)`. |
| `instructions.md` | Non-empty root instructions. Managed `Agent` definitions may omit `instruction`; the compiler supplies this file. |
| `requirements.txt` | Provider and agent-specific Python dependencies. Do not add Harnest itself. |

The usual entrypoint is `agent:root_agent`. Authored source is not a Python
package and does not need `__init__.py`.

## Discovered resource folders

| Path | Contract |
| --- | --- |
| `tools/<name>.py` | Exports one `@tool`-decorated callable named `<name>`. |
| `subagents/<name>.py` | Exports one `AgentDefinition` named `<name>` with an explicit instruction. |
| `subagents/<name>/agent.py` | Recursively composed subagent named `<name>` with its own `instructions.md`, tools, subagents, MCP clients, sandbox, and skills. Plugins and extensions remain root-only. |
| `mcp/<name>.py` | Exports `MCPClient` or `None` as `<name>`. `None` disables an optional connection. |
| `plugins/<plugin>/mcp/<name>.py` | Plugin-owned MCP client using the same export rule. |
| `plugins/<plugin>/skills/<skill>/SKILL.md` | One progressive skill teaching the host agent when and how to use the plugin's MCP tools. |
| `extensions/<name>/lifecycle.py` | Exports portable `Extension` as `extension`; its declared name matches the folder. |
| `extensions/<name>/adk.py` | Optional ADK `BasePlugin` exported as `extension`. |
| `extensions/<name>/langgraph.py` | Optional LangChain `AgentMiddleware` exported as `extension`. |
| `sandbox/sandbox.py` | Exports one `Sandbox` as `sandbox`; managed ADK only. |
| `skills/<skill>/SKILL.md` | Progressive internal instructions. Frontmatter `name` matches `<skill>`; references, assets, and scripts may live below it. |
| `evals/<id>.evalset.json` | ADK `EvalSet` whose ID matches the filename. Optional `evals/test_config.json` configures evaluation. |
| `tests/unit/test_*.py` | Offline authored tests. |
| `tests/smoke/test_*.py` | Explicitly enabled live model, MCP, or HTTP tests. |

Plugins contain only MCP clients and skills. A non-empty plugin must have at
least one MCP module and one skill. Plugins never contain agents or lifecycle
behavior. Use `subagents/` for agents and `extensions/` for lifecycle behavior.

## Discovery invariants

- Missing, empty, ignored-only folders are skipped.
- Once a public resource exists, the full convention is strict.
- Resource discovery is deterministic by path name.
- Duplicate tool, MCP configuration, subagent, or skill identities fail.
- Public symlinks are rejected so compiled artifacts remain self-contained.
- Do not edit or commit `.harnest/`; it is disposable compiler output.
- Runtime `skills/` are not the same as `.agents/skills/harnest-authoring/`,
  which teaches a coding agent how to modify this project.
