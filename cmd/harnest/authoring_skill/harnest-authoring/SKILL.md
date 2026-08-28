---
name: harnest-authoring
description: Build, modify, test, or review Harnest agents and source. Use for agent folders, harnest.* imports, libraries, ADK or LangGraph graphs, client tools, plugins, extensions, MCP, skills, evals, compilation, serving, and Harnest source changes.
---

# Harnest authoring

Produce tested agents. Never edit generated
`.harnest/` artifacts.

Not a runtime skill.

## Modify safely

1. Preserve unrelated changes. For a legacy project, run read-only `harnest
   upgrade AGENT_DIR` before editing; apply only after reviewing it. Never run
   `init` over existing work.
   Init uses ignored guides; request `--example` for full samples.
2. Preserve framework, mode, and `Agent.history` unless the user requests
   architectural migration.
   Preserve both storage factories; read `docs/checkpoints.md` before changing
   checkpoint ownership.
3. Put each capability beside its owning `agent.py`. Managed resources are
   discovered; do not import or manually register sibling tools, MCP clients,
   plugins, skills, or subagents. Nested agents do not inherit parent resources.
4. Keep authored imports side-effect free. Put Pydantic contracts in root
   `models/` and code in root `lib/`; import via `harnest.models.*` and
   `harnest.lib.*`. Neither needs `__init__.py`. Publish values with `@context`.
5. Import authoring symbols explicitly from `harnest.*`; no magic globals or
   compatibility aliases exist.
6. Match path/export contracts. MCP `client()` factories take no parameters;
   `@client_tool` stubs run in callers, never the agent server.
   Use `@lifecycle.*` on extension listeners; keep helpers ignored. Put MCP
   approval on `client()` and name original remote tools. Add one root
   `@lifecycle.output_policy` only when provisional subagent narration should
   be public. Invalid resources fail.
7. Put agent dependencies in `pyproject.toml`; never add Harnest or framework
   packages. Put deployment settings in `config.yaml`,
   and standalone HTTP policy in `server.yaml`; use exact `${NAME}` references
   for startup environment values. Put public identity in `agent-card.yaml`.
   Run `harnest env sync`; commit `uv.lock`. Upgrade Harnest for newer frameworks.

## Load guidance

- Read [references/folder-edits.md](references/folder-edits.md) before changing
  structure or capability ownership.
- Read [references/layout.md](references/layout.md) for path contracts.
- Read [references/python-api.md](references/python-api.md) for Python APIs.
- Use `$harnest-authentication` for auth or credential changes.
- Read [references/frameworks.md](references/frameworks.md) for graphs,
  frameworks, modes, models, and subagents.
- Read [references/workflows.md](references/workflows.md) for installation,
  migration, tests, evals, compilation, and serving.
- Read [references/quality.md](references/quality.md) when contributing to
  Harnest source.

## Finish with evidence

Run focused tests, then `harnest test AGENT_DIR` and compile after structural
changes. Evals default to business trajectories; use strict when exact tool
calls matter. Use smoke only for authorized live calls. Report the
framework/mode, checks, live calls, and remaining provider requirements. Treat
compiler diagnostics as authoritative.
