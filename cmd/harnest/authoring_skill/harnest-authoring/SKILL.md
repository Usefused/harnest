---
name: harnest-authoring
description: Build, modify, test, review Harnest agents and source. Use for agent folders, harnest.* imports, libraries, ADK or LangGraph graphs, durable tools, queued/cron tasks, Harnest Extensions, continuations, agent-plugins, lifecycle, MCP, static or dynamic skills, evals, compilation, running, serving, and Harnest source changes.
---

# Harnest authoring

Produce agents. Never edit `.harnest/`.

## Modify safely

1. Preserve unrelated changes. For legacy projects, inspect `harnest upgrade
   AGENT_DIR` before editing. Never run `init` over existing work; request
   `--example` for samples.
2. Preserve framework, mode, and `Agent.history` unless migration is requested.
   Preserve session/checkpoint authorities; read `docs/checkpoints.md`
   before changing checkpoint ownership.
3. Put capabilities beside their owning `agent.py`. Managed discovery handles
   tools, MCP clients, Agent Plugins, skills, and subagents; do not import or
   register them manually. Use extension public APIs without registration.
   Nested agents do not inherit parent resources.
4. Keep authored imports side-effect free. Put Pydantic contracts in root
   `models/` and code in root `lib/`; import via `harnest.models.*` and
   `harnest.lib.*`. Neither needs `__init__.py`. Publish values with `@context`.
   Inline media is transient; `Stored(...)` requires named storage and an async
   `@tool`.
5. Import authoring symbols from `harnest.*`; prefer canonical APIs over legacy compatibility aliases.
6. Match path/export contracts. MCP `client()` factories take no arguments;
   `@client_tool` stubs run in callers, never the agent server. Decorate
   lifecycle listeners with `@lifecycle.*`; tool/HTTP interceptors return
   `context.next(...)` or `context.finish(...)`. Keep helpers ignored. Put MCP
   approval on remote tools. Describe each tool argument's semantic role;
   expose pagination and ordering as typed arguments rather than prompt hints.
   Invalid resources fail.
7. Put agent dependencies in root `pyproject.toml`; Harnest Extensions may own a
   matching PEP 621 project but never add Harnest/framework packages. Put deployment and optional `server` settings in `config.yaml`; use exact `${NAME}` references
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

Run focused tests, then `harnest test AGENT_DIR`; compile after structural
changes. Use strict evals for exact tool calls and smoke only for authorized
live calls. Report framework/mode, checks, live calls, and remaining
requirements.
