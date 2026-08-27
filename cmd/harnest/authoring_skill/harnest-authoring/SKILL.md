---
name: harnest-authoring
description: Build, modify, debug, test, or review a filesystem-first Harnest agent project. Use for Harnest agent folders, compiler-owned harnest.* Python imports, managed ADK or LangGraph graphs, plugins, extensions, MCP clients, skills, evals, tests, compilation, and standalone serving.
---

# Harnest authoring

Treat an authored agent directory as compiler input. Edit its source files, then
let `harnest` discover, validate, compose, and lower them. Never edit generated
`.harnest/` artifacts.

Do not confuse this coding-agent skill with an authored agent's `skills/`
directory. The latter contains progressive runtime instructions for the agent
being built.

## Work safely

1. Read `config.yaml`, `agent.py`, `instructions.md`, and the relevant resource
   folders before changing the project.
2. Preserve the selected `spec.framework.name` (`adk` or `langgraph`) and mode
   (`managed` or `advanced`) unless the user asks to change architecture.
3. Put each capability in its conventional folder. Do not import sibling tools,
   MCP clients, plugins, skills, or flat subagents into `agent.py`; the compiler
   discovers them. Graph nodes must still be referenced explicitly in the graph.
4. Import every authoring symbol explicitly from `harnest.*`. There are no magic
   globals and no compatibility layer.
5. Match filename, export name, and declared resource name where the convention
   requires it. Populated invalid folders fail compilation; empty folders are
   skipped.
6. Keep provider packages in `requirements.txt`, configuration in `config.yaml`,
   secrets in declared secret references or runtime environment, and public
   identity in `agent-card.yaml`. Keep ADK or LangGraph inside the version range
   supported by the installed Harnest release; do not widen it independently.
7. Run the narrowest useful checks, then `harnest test <agent-dir>`. Add
   `--smoke` only for authorized live calls and `--evals` when eval assets exist.
   Compile after structural changes to catch composition and backend errors.

## Read the relevant reference

- Read [references/layout.md](references/layout.md) before adding, moving, or
  removing files or folders.
- Read [references/python-api.md](references/python-api.md) when authoring with
  a `harnest.*` namespace or choosing between plugins and extensions.
- Read [references/frameworks.md](references/frameworks.md) when changing graph
  structure, framework, managed/advanced mode, models, subagents, or sandboxing.
- Read [references/workflows.md](references/workflows.md) for CLI validation,
  testing, compilation, serving, and a final change checklist.

Prefer the smallest change that satisfies the request. Let compiler diagnostics
define the contract when authored source and remembered documentation disagree.
