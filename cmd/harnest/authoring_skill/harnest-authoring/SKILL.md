---
name: harnest-authoring
description: Build, modify, test, or review Harnest agents and source. Use for agent folders, compiler-owned harnest.* imports, libraries, managed ADK or LangGraph graphs, client tools, plugins, extensions, MCP clients, skills, evals, tests, compilation, standalone serving, and Harnest Python or Go changes.
---

# Harnest authoring

Produce tested agents through filesystem ownership. Never edit generated
`.harnest/` artifacts.

Keep this skill separate from deployed runtime `skills/`.

## Modify safely

1. Preserve unrelated changes. For a legacy project, run read-only `harnest
   upgrade AGENT_DIR` before editing; apply only after reviewing its plan. Read
   contracts and affected folders. Never run `init` over existing work.
   Init uses ignored guides; request `--example` for full samples.
2. Preserve framework, mode, and `Agent.history` unless the user requests
   architectural migration.
   Preserve both storage factories; read `docs/checkpoints.md` before changing
   checkpoint ownership.
3. Put each capability beside its owning `agent.py`. Managed resources are
   discovered; do not import or manually register sibling tools, MCP clients,
   plugins, skills, or subagents. Nested agents do not inherit parent resources.
4. Compilation executes authored modules. Keep imports side-effect free. Put
   shared code in root `lib/` without required `__init__.py`; import via
   `harnest.lib.*`. Publish runtime values with `@context`.
5. Import authoring symbols explicitly from `harnest.*`; no magic globals or
   compatibility aliases exist.
6. Match path/export contracts. MCP `client()` factories take no parameters;
   `@client_tool` stubs run in callers, never the agent server.
   Use `@lifecycle.*` on extension listeners; keep helpers ignored. Put MCP
   approval on `client()` and name original remote tools. Invalid resources
   fail compile.
7. Put agent dependencies in `pyproject.toml`; never add Harnest or framework
   packages. Put deployment settings in `config.yaml`,
   and standalone HTTP policy in `server.yaml`; use exact `${NAME}` references
   for startup environment values. Put public identity in `agent-card.yaml`.
   Run `harnest env sync`; commit `uv.lock`. Upgrade Harnest for newer frameworks.

## Load only relevant guidance

- Read [references/folder-edits.md](references/folder-edits.md) before changing
  structure or capability ownership.
- Read [references/layout.md](references/layout.md) for path contracts.
- Read [references/python-api.md](references/python-api.md) for Python APIs.
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
