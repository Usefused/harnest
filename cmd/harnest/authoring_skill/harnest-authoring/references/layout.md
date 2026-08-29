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
| `pyproject.toml` | Agent, runtime-plugin, and provider dependencies synchronized by Harnest. Never add Harnest, ADK, LangGraph, or Harnest-owned framework adapters. |
| `uv.lock` | Resolved dependency lock created by `harnest env sync`; commit it after review. |

The usual entrypoint is `agent:root_agent`. Authored source is not a Python
package and does not need `__init__.py`.

## Reusable authored modules

| Path | Contract |
| --- | --- |
| root `models/**/*.py` | Pydantic contracts imported below `harnest.models`; never discovered as a capability. |
| root `lib/**/*.py` | Ordinary reusable Python imported below `harnest.lib`; never discovered as a tool, agent, MCP client, runtime plugin, agent-plugin, extension, or skill. |

`models/` and `lib/` are root-only and global to the compiled bundle in managed
and advanced mode. `models/support.py` imports as `harnest.models.support`;
`lib/audit.py` imports as `harnest.lib.audit`, while
`lib/storage/queries.py` imports as `harnest.lib.storage.queries`. Namespace
packages need no `__init__.py`; do not import them as bare `models.*` or
`lib.*`. Add an initializer only for intentional package initialization. Entry
points and resources at any ownership depth may use both namespaces.
The same imports work during compilation, tests, evals, and standalone serving.

## Discovered resource folders

| Path | Contract |
| --- | --- |
| `tools/<name>.py` | Exports one `@tool`-decorated callable named `<name>`. |
| root `tasks/<name>.py` | Exports one `@task`-decorated callable named `<name>`. Tasks are application-owned queue work and are not model tools. |
| `subagents/<name>.py` | Exports one managed `Agent` with an explicit instruction or one native `Agent.advanced(...)`, named `<name>`. |
| `subagents/<name>/agent.py` | Recursively composed managed subagent named `<name>` with its own folder-scoped `instructions.md`, tools, MCP clients, sandbox, and skills. Advanced subagents use the flat-file form because native source owns composition. ADK also permits child subagents; LangGraph does not. Runtime plugins, agent-plugins, and extensions remain root-only. |
| `mcp/<name>.py` | Exports one literally zero-parameter `client()` factory returning `MCPClient`; `<name>` is its local identity. |
| `plugins/<name>/plugin.yaml` | Marks a `RuntimePlugin`; its `metadata.name` matches the identifier-safe folder name and its entrypoint is `plugin:plugin`. |
| `plugins/<name>/plugin.py` | Exports one public local `Plugin` subclass and the singleton `plugin` instance. The module is exposed as `harnest.plugins.<name>`. |
| `plugins/<name>/pyproject.toml` | Optional PEP 621 project whose name/version match `plugin.yaml` and whose static dependencies join the root environment solve. It never creates a private plugin environment. |
| `plugins/<name>/lib/**/*.py` | Private plugin helpers. `plugin.py` may import `.lib.client`; contributed tools/extensions import `harnest.plugins.<name>.lib.client`. Application-wide helpers remain in root `lib/` and import through `harnest.lib.*`. |
| `plugins/<name>/extensions/**/*.py` | Runtime-plugin lifecycle, context, storage, route, or native contributions declared by the manifest. They join root and dependency-ordered plugin extensions in one globally validated lifecycle. |
| `plugins/<name>/mcp/<client>.py` | Plugin-owned zero-parameter `client()` factory. In a manifest-less folder this is agent-plugin content and must be paired with a skill. |
| `plugins/<name>/skills/<skill>/SKILL.md` | Progressive guidance. In a manifest-less folder this is agent-plugin content and must be paired with an MCP client. |
| `extensions/storage.py` | Generated home for required session/checkpoint factories. Stack `@lifecycle.storage.sessions` and `.checkpoints` when one store owns both roles, or split them across arbitrary extension files. Import a root `harnest.lib.*` helper only when reuse warrants it. |
| `extensions/credentials.py` | Optional root `@lifecycle.credential_provider` factory returning one private `CredentialProvider`; never publish it with `@context`. |
| `extensions/telemetry.py` | Optional repeatable root `@lifecycle.telemetry_exporter` factories, one uniquely named trace/log destination per factory; factories run only at runtime. |
| `extensions/**/*.py` | Other arbitrary public root modules; explicit `@lifecycle.*` listeners and `@context` providers are discovered. Multiple listeners may share an invocation phase. |
| `sandbox/sandbox.py` | Exports one `Sandbox` as `sandbox`; managed ADK only. |
| `skills/<skill>/SKILL.md` | Progressive internal instructions. Frontmatter `name` matches `<skill>`; references, assets, and scripts may live below it. |
| root `evals/<id>.evalset.json` | ADK `EvalSet` whose ID matches the filename. Optional root `evals/test_config.json` configures evaluation. |
| `tests/unit/test_*.py` | Offline authored tests. |
| `tests/smoke/test_*.py` | Explicitly enabled live model, MCP, or HTTP tests. |

`plugin.yaml` distinguishes the two plugin contracts. A runtime plugin owns
same-process Python behavior and declared content. A manifest-less folder is an
**agent-plugin**: it contains only MCP clients plus skills, must contain at least
one of each, and never owns agents or lifecycle behavior.

## Runtime-plugin descriptor and export

Keep the descriptor closed and explicit:

```yaml
apiVersion: harnest.dev/v1alpha1
kind: RuntimePlugin
metadata:
  name: temporal
  version: 1.0.0
runtime:
  entrypoint: plugin:plugin
requires:
  plugins: [core]
capabilities:
  - lifecycle.tool
  - context.continuations
  - context.storage
```

`requires.plugins` names local runtime plugins. Harnest orders dependencies
before dependants for startup and reverses that order for shutdown; missing
dependencies and cycles fail compilation. `capabilities` declares the bounded
Harnest lifecycle, context, content, storage, HTTP, native, policy, or telemetry
surfaces the plugin contributes. Declare only surfaces the plugin actually
uses; declaration does not grant access outside Harnest-owned boundaries.
`context.continuations` is for runtime plugins that adapt an external durable
runtime. It requires a Harnest-owned portable checkpoint store and does not
make the provider's job identifiers public.

```python
from harnest.plugins import Plugin


class Temporal(Plugin):
    pass


plugin = Temporal()
```

Both the public class and singleton must be local exports of `plugin.py`.
Consumers import them from `harnest.plugins.temporal`. A plugin may extend
`PluginContext`; its singleton's `.context` is invocation-scoped and revoked
after the call. Application hooks `start(context)` and `stop()` are async.

Runtime plugins install no private environment. Root code, runtime plugins, and
agent-plugins execute in the same interpreter and event loop. A runtime plugin
may own a PEP 621 `pyproject.toml` when packaging its SDK-facing dependencies;
Harnest resolves those constraints jointly with the root project before any
plugin or agent import. The plugin project name and version must match
`plugin.yaml`, dependencies must be static, and compiler-owned Harnest or
framework packages remain forbidden. Use `harnest.lib.*` for
application-owned helpers rather than packaging the application as a plugin.

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
- Runtime plugins, agent-plugins, and root extensions are root-only; nested
  instances fail compilation.
- `models/` and `lib/` are also root-only, but their modules are globally
  importable throughout the bundle rather than attached to an agent's
  discovered resource scope.
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
  same-named direct, agent-plugin, runtime-plugin, and subagent clients remain
  distinct.
- Root and runtime-plugin extensions are flattened in dependency order, then
  validated as one application. Duplicate singleton authorities, named
  resources, routes, native integrations, and context values fail globally.
- Library modules are copied and importable, but their callables are never
  discovered or injected into an agent.
- Public symlinks are rejected so compiled artifacts remain self-contained.
- Compilation validates `server.yaml` and copies a mutable operational copy
  beside `harnest-agent`; the authored copy remains under `source/`. Exact
  `${NAME}` values are preserved and resolved only when the launcher starts.
- Do not edit or commit `.harnest/`; it is disposable compiler output.
- Runtime `skills/` are not the same as `.agents/skills/harnest-authoring/`,
  which teaches a coding agent how to modify this project.
