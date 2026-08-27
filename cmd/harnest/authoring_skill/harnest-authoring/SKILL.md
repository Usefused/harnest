---
name: harnest-authoring
description: Build, modify, debug, test, or review a filesystem-first Harnest agent project or contribute to Harnest itself. Use for Harnest agent folders, compiler-owned harnest.* Python imports, reusable lib code, managed ADK or LangGraph graphs, plugins, extensions, MCP clients, skills, evals, tests, compilation, standalone serving, and Harnest Python or Go source changes.
---

# Harnest authoring

Produce a valid, tested Harnest agent without bypassing filesystem ownership.
Edit authored source, then let `harnest` discover, compose, and lower it. Never
edit generated `.harnest/` artifacts.

Keep this coding-agent skill separate from authored runtime `skills/`, which
teach the deployed agent how to perform tasks.

## Modify safely

1. Preserve unrelated changes. Read `config.yaml`, `agent.py`,
   `instructions.md`, and the affected folders before editing. Never run
   `harnest init` over an existing project.
   New `init` projects use ignored guides; request `--example` for full samples.
2. Preserve `spec.framework.name` and mode unless the user requests an
   architectural migration.
3. Put each capability beside its owning `agent.py`. Managed resources are
   discovered; do not import or manually register sibling tools, MCP clients,
   plugins, skills, or subagents. Nested agents do not inherit parent resources.
4. Put shared ordinary Python in root `lib/` and import it as `harnest.lib.*`.
   Do not use `lib/` for a discovered capability; `__init__.py` is unnecessary.
5. Import authoring symbols explicitly from `harnest.*`; no magic globals or
   compatibility aliases exist.
6. Match required paths, exports, and declared names. Missing or empty optional
   folders are skipped; populated invalid folders fail compilation.
7. Put dependencies in `requirements.txt`, deployment settings in `config.yaml`,
   secrets in environment/secret references, and public identity in
   `agent-card.yaml`. Keep framework versions within this Harnest release's
   supported range.

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
