---
name: harnest-authoring
description: Build, modify, test, or review Harnest agents and source. Use for agent folders, harnest.* imports, libraries, ADK or LangGraph graphs, durable tools, queued tasks, runtime plugins, continuations, agent-plugins, extensions, MCP, static or dynamic skills, evals, compilation, serving, and Harnest source changes.
---

# Harnest authoring

Produce agents. Never edit `.harnest/`. Not a runtime skill.

## Modify safely

1. Preserve unrelated changes. For legacy projects, inspect `harnest upgrade
   AGENT_DIR` before editing. Never run `init` over existing work; request
   `--example` for samples.
2. Preserve framework, mode, and `Agent.history` unless migration is requested.
   Preserve session/checkpoint authorities; read `docs/checkpoints.md`
   before changing checkpoint ownership.
3. Put each capability beside its owning `agent.py`. Managed resources are
   discovered; do not import or manually register sibling tools, MCP clients,
   runtime plugins, agent-plugins, skills, or subagents. Nested agents do not
   inherit parent resources.
4. Keep authored imports side-effect free. Put Pydantic contracts in root
   `models/` and code in root `lib/`; import via `harnest.models.*` and
   `harnest.lib.*`. Neither needs `__init__.py`. Publish values with `@context`.
   Inline media is transient; `Stored(...)` requires named storage and an async
   `@tool`.
5. Import authoring symbols from `harnest.*`; no magic globals or
   compatibility aliases exist.
6. Match path/export contracts. MCP `client()` factories take no arguments;
   `@client_tool` stubs run in callers, never the agent server. Decorate
   extension listeners with `@lifecycle.*`; tool/HTTP interceptors return
   `context.next(...)` or `context.finish(...)`. Keep helpers ignored. Put MCP
   approval on remote tools. Invalid resources fail.
7. Put agent dependencies in root `pyproject.toml`; runtime plugins may own a
   matching PEP 621 project but never add Harnest/framework packages. Put deployment settings in `config.yaml`,
   and standalone HTTP policy in `server.yaml`; use exact `${NAME}` references
   for startup environment values. Put public identity in `agent-card.yaml`.
   Run `harnest env sync`; commit `uv.lock`. Upgrade Harnest for frameworks.

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
