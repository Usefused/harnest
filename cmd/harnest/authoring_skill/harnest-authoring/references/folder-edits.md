# Modifying an existing Harnest project

Read this playbook before adding, moving, renaming, or deleting agent resources.
The filesystem is part of the program: a correct Python object in the wrong
folder has different ownership or may not compile at all.

## Establish the edit boundary

1. Check the working tree and preserve unrelated edits. Do not regenerate an
   initialized project.
2. Read `config.yaml` first. Record the selected framework and whether the mode
   is `managed` or `advanced`; that decision changes how capabilities are wired.
3. Inventory authored files with `rg --files`, excluding `.harnest/`. Read the
   root `agent.py`, `instructions.md`, `server.yaml` when serving behavior is in
   scope, and only the resource folders involved in the requested change.
4. Identify the owning `agent.py`. The root owns root sibling resources. A
   folder agent at `subagents/<name>/agent.py` owns supported folders beside
   that file. A flat `subagents/<name>.py` cannot own private resources.
5. Search the agent source, tests, skills, and evals for the old resource name
   before a rename, move, or deletion. Graph string nodes are real references.

Do not infer ownership from a tool list on `Agent`. In managed mode, location is
the access boundary and the compiler performs composition.

Keep every authored `SKILL.md` at 400 words or fewer, including frontmatter.
State the outcome and actions in `SKILL.md`; move conditional detail, schemas,
and extended examples into that skill's linked `references/` files.

## Route the change

| Intent | Managed-mode location | Important follow-up |
| --- | --- | --- |
| Change the agent's general behavior | Owning `instructions.md` | Keep it non-empty; do not duplicate it into `agent.py` unless dynamic instructions are required. |
| Add one callable capability | Owning `tools/<name>.py` | Export one `@tool` callable named exactly `<name>`. |
| Add model-facing operational guidance | Owning `skills/<skill>/SKILL.md` | Give it distinct frontmatter name/description and put supporting files below that skill. |
| Connect one MCP server | Owning `mcp/<name>.py` | Export `<name>` as `MCPClient` or `None`; declare credentials through environment/secrets. |
| Bundle reusable MCP access plus usage guidance | Root `plugins/<plugin>/mcp/` and `plugins/<plugin>/skills/` | A populated plugin needs at least one MCP client and one skill; it never contains agents or lifecycle code. |
| Add a simple subagent | Owning `subagents/<name>.py` | Export `AgentDefinition` as `<name>` with an explicit instruction. |
| Add a subagent with private tools, MCP, sandbox, or skills | Owning `subagents/<name>/agent.py` plus sibling resources | Add that folder's non-empty `instructions.md`; do not expect parent resources to inherit. |
| Add invocation policy, persistence, guardrails, or event transforms | Root `extensions/<name>/lifecycle.py` | Export `extension`; add `adk.py` or `langgraph.py` only for tighter native control. |
| Share ordinary Python across resources | Root `lib/**/*.py` | Import below `harnest.lib`; it is global library code, not a discovered resource. |
| Add code execution isolation | Owning `sandbox/sandbox.py` | Managed ADK only; export `sandbox` and declare provider dependencies. |
| Add offline behavior coverage | Root `tests/unit/test_*.py` | Use compiler fixtures; do not manually import the compiled agent. |
| Add authorized live coverage | Root `tests/smoke/test_*.py` | Keep external calls behind the smoke lane. |
| Add ADK evaluations | Root `evals/` | Keep executable evals at root; expected responses contain visible output only. |
| Change public identity or advertised capability | Root `agent-card.yaml` | Do not use the card as runtime wiring. |
| Change resources, environment, framework, mode, or entrypoint | Root `config.yaml` | Treat framework/mode changes as migrations, not incidental edits. |
| Change standalone host, request limits, concurrency, timeout, or playground | Root `server.yaml` | Keep auth, storage, TLS, secrets, and deployment scaling outside this file. |

## Wire managed resources correctly

- A managed `Agent` receives resources discovered beside its owning `agent.py`.
  Do not import those sibling modules or repeat them in `tools=`, `mcp=`,
  `subagents=`, or skill name lists.
- A managed `Graph` must consume discovered capabilities. Put an `Agent` node in
  the relevant ownership scope or use a string node naming a discovered tool or
  subagent. Compilation rejects discovered resources that no graph node uses.
- Use normal Python imports only for installed dependencies and stable authored
  library modules. A Harnest agent root is not itself a Python package, and
  resource modules are loaded independently by the compiler.
- Put pure helpers, domain types, validation, and reusable clients in root
  `lib/`, then import them below `harnest.lib`. For example,
  `lib/storage/queries.py` is `harnest.lib.storage.queries`. Do not add
  `__init__.py` just to form packages, import one discovered resource from
  another, or place `lib/` beneath a subagent. The root library is available to
  every bundle resource in managed and advanced mode during compile, tests,
  evals, and standalone runs.
- Cross-cutting invocation policy still belongs in a portable lifecycle
  extension. Use `lib/` for implementation shared by resources, not to disguise
  persistence, auditing, or guardrails that must surround every call.

## Treat advanced mode differently

Advanced mode owns all framework wiring in `agent.py`. Harnest deliberately
rejects populated managed-discovery folders (`tools/`, `subagents/`, `mcp/`,
`plugins/`, `extensions/`, `sandbox/`, `skills/`, and `evals/`) rather than
silently ignoring them.

When editing an advanced project:

- import ADK or LangGraph and dependencies explicitly in `agent.py` or an
  intentional Python package;
- wire tools, middleware, agents, state, and lifecycle into the native target;
- keep `Agent.advanced(...)` as the Harnest boundary; and
- use root `tests/` for coverage.

Before migrating an edited managed project, run `harnest mode advanced
AGENT_DIR --check`. Apply its report by hand; do not run a generator over the
project or move files until their new explicit owner is clear.

## Make structural changes without losing work

### Add

Create the smallest public resource that owns the new behavior. Match the file
stem, Python export, declared name, and skill frontmatter where required. Add a
focused unit test before expanding into smoke or eval coverage.

### Extract duplicated implementation

Keep each discovered resource independently owned while sharing its ordinary
implementation:

1. Create the smallest root `lib/<path>.py` module that names the shared domain
   responsibility, not the callers that happen to use it.
2. Move only reusable functions, types, or clients. Leave `@tool`, `Agent`, MCP,
   extension, and other resource declarations in their convention folders.
3. Replace duplication with explicit `harnest.lib.<path>` imports. No
   `__init__.py` is required because Harnest supplies namespace packages.
4. Run the affected unit tests through `harnest test`, then compile. This proves
   the compiler-mounted import works instead of relying on the current shell's
   Python path.

### Rename or move

Treat a path move as an ownership change:

1. Search for graph strings, test fixtures, eval references, documentation, and
   declared names that use the old identity.
2. Move the existing implementation without rewriting unrelated logic.
3. Rename the exported object and declared identity to match the destination.
4. Update consumers in the same change and compile immediately. Never leave the
   old and new public resource present together as a compatibility shim.

### Promote a flat subagent to a folder

Move `subagents/<name>.py` to `subagents/<name>/agent.py`, keep the export named
`<name>`, add `subagents/<name>/instructions.md`, and then add only that agent's
private resource folders beside it. Update explicit graph references only if
the subagent identity changed; the path-shape change alone keeps the same name.

### Delete

Search for every consumer first, remove the public resource and its references
in one change, and update tests/evals that described the removed capability.
An empty optional directory may remain because the compiler skips it; remove it
only when doing so does not discard placeholders or user documentation.

## Validate the modification

Run focused unit tests while editing, then:

```bash
harnest test AGENT_DIR
harnest compile AGENT_DIR --output AGENT_DIR/.harnest/check
```

Use `--evals` when root eval assets changed. Use `--smoke` only when the user has
authorized live model, MCP, or network calls. A successful Python import is not
enough: compilation is what verifies filesystem ownership and resource
consumption.
