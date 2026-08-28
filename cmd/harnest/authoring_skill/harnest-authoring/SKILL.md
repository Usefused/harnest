---
name: harnest-authoring
description: Build, modify, test, or review a filesystem-first Harnest agent project or Harnest itself. Use for agent folders, compiler-owned harnest.* imports, libraries, managed ADK or LangGraph graphs, client tools, plugins, extensions, MCP clients, skills, evals, tests, compilation, standalone serving, and Harnest Python or Go changes.
---

# Harnest authoring

Produce tested agents through filesystem ownership; let `harnest` compose
authored source. Never edit generated `.harnest/` artifacts.

Keep this coding skill separate from runtime `skills/` used by deployed agents.

## Modify safely

1. Preserve unrelated changes. For a legacy project, run read-only `harnest
   upgrade AGENT_DIR` before editing; apply only after reviewing its plan. Read
   contracts and affected folders. Never run `init` over existing work.
   Init uses ignored guides; request `--example` for full samples.
2. Preserve framework, mode, and `Agent.history` unless the user requests
   architectural migration.
3. Put each capability beside its owning `agent.py`. Managed resources are
   discovered; do not import or manually register sibling tools, MCP clients,
   plugins, skills, or subagents. Nested agents do not inherit parent resources.
4. Put shared ordinary Python in root `lib/` and import it as `harnest.lib.*`.
   Do not use `lib/` for a discovered capability; `__init__.py` is unnecessary.
5. Import authoring symbols explicitly from `harnest.*`; no magic globals or
   compatibility aliases exist.
6. Match path/export contracts. MCP `client()` factories take no parameters;
   `@client_tool` stubs run in callers, never the agent server.
   Decorate executable extension listeners with `@lifecycle.*`; helpers stay
   ignored. Invalid resources fail compile.
7. Put agent dependencies in `pyproject.toml`; never add Harnest or framework
   packages. Put deployment settings in `config.yaml`,
   and standalone HTTP policy in `server.yaml`; use exact `${NAME}` references
   for startup environment values. Put public identity in `agent-card.yaml`.
   Run `harnest env sync`; commit `uv.lock`. Upgrade Harnest for newer frameworks.

## Load only relevant guidance

- Read [references/folder-edits.md](references/folder-edits.md) before changing
  structure or capability ownership.
- Read [references/layout.md](references/layout.md) for path contracts.
- Read [references/python-api.md](references/python-api.md) for `harnest.*`,
  plugins, extensions, telemetry, MCP, and sandbox APIs.
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
