# Harnest project layout

Read this reference when adding, moving, or removing authored resources.
Use [folder-edits.md](folder-edits.md) for the safe modification workflow; this
file is the path and ownership contract it relies on.

## Required root files

| Path | Purpose |
| --- | --- |
| `config.yaml` | Deployment resources, runtime environment, entrypoint, framework, mode, and optional root `server` overrides. Omitted server settings use defaults. |
| `agent-card.yaml` | Public agent identity, interfaces, capabilities, and advertised A2A skills. |
| `agent.py` | Exports a managed `Agent`/portable `Graph`, or an `Agent` created with `Agent.advanced(...)`. |
| `instructions.md` | Non-empty root instructions. Managed `Agent` definitions may omit `instruction`; the compiler supplies this file. |
| `pyproject.toml` | Agent, Harnest Extension, and provider dependencies synchronized by Harnest. Never add Harnest, ADK, LangGraph, or Harnest-owned framework adapters. |
| `uv.lock` | Resolved dependency lock created by `harnest env sync`; commit it after review. |

Set `server.live: true` in `config.yaml` to enable WebSockets on the HTTP listener.
New project defaults disable live transport. Legacy `server.yaml` files without
`live` retain it; set `live: false` there to disable all WebSocket upgrades.

The usual entrypoint is `agent:root_agent`. Authored source is not a Python
package and does not need `__init__.py`.

## Reusable authored modules

| Path | Contract |
| --- | --- |
| root `models/**/*.py` | Pydantic contracts imported below `harnest.models`; never discovered as a capability. |
| root `lib/**/*.py` | Ordinary reusable Python imported below `harnest.lib`; never discovered as a tool, agent, MCP client, Harnest Extension, agent-plugin, extension, or skill. |

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
| root `cron/<name>.py` | Exports one same-named `Cron` that targets a discovered root task. Schedules use five fields and UTC. |
| `subagents/<name>.py` | Exports one managed `Agent` with an explicit instruction or one native `Agent.advanced(...)`, named `<name>`. |
| `subagents/<name>/agent.py` | Recursively composed managed subagent named `<name>` with its own folder-scoped `instructions.md`, tools, MCP clients, sandbox, and skills. Advanced subagents use the flat-file form because native source owns composition. ADK also permits child subagents; LangGraph does not. Harnest Extensions, Agent Plugins, and lifecycle hooks remain root-only. |
| `mcp/<name>.py` | Exports one literally zero-parameter `client()` factory returning `MCPClient`; `<name>` is its local identity. |
| `extensions/<name>/extension.yaml` | Marks an `Extension`; its `metadata.name` matches the identifier-safe folder name and its entrypoint is `extension:extension`. |
| `extensions/<name>/extension.py` | Exports one public local `Extension` subclass and the singleton `extension` instance. The module is exposed as `harnest.extensions.<name>`. |
| `extensions/<name>/pyproject.toml` | Optional PEP 621 project whose name/version match `extension.yaml` and whose static dependencies join the root environment solve. It never creates a private extension environment. |
| `extensions/<name>/lib/**/*.py` | Private extension helpers. `extension.py` may import `.lib.client`; contributed tools and lifecycle hooks import `harnest.extensions.<name>.lib.client`. Application-wide helpers remain in root `lib/` and import through `harnest.lib.*`. |
| `extensions/<name>/lifecycle/**/*.py` | Harnest Extension lifecycle, context, storage, route, or native contributions declared by the manifest. They join root and dependency-ordered extension contributions in one globally validated lifecycle. |
| `plugins/<name>/plugin.json` | Required Agent Plugins 1.0 manifest with canonical `$schema` and a valid `name`; the manifest supplies identity. |
| `plugins/<name>/mcp.json` | Optional standard MCP configuration with canonical `$schema`, `mcpServers`, and explicit transport types. No Python factories. |
| `extensions/<name>/mcp/<client>.py` | Harnest Extension MCP client, requiring the `content.mcp` capability. |
| `plugins/<name>/skills/<skill>/SKILL.md` | Optional portable Agent Plugin guidance; no MCP server is required. |
| `extensions/<name>/skills/<skill>/SKILL.md` | Harnest Extension guidance, requiring the `content.skills` capability. |
| `lifecycle/storage.py` | Generated home for required session/checkpoint factories. Stack `@lifecycle.storage.sessions` and `.checkpoints` when one store owns both roles, or split them across arbitrary lifecycle files. Import a root `harnest.lib.*` helper only when reuse warrants it. |
| `lifecycle/credentials.py` | Optional root `@lifecycle.credential_provider` factory returning one private `CredentialProvider`; never publish it with `@context`. |
| `lifecycle/telemetry.py` | Optional repeatable root `@lifecycle.telemetry_exporter` factories, one uniquely named trace/log destination per factory; factories run only at runtime. |
| `lifecycle/**/*.py` | Other arbitrary public root modules; explicit `@lifecycle.*` listeners and `@context` providers are discovered. Multiple listeners may share an invocation phase. |
| `sandbox/<name>.py` | Exports a framework-neutral `Sandbox` variable matching `<name>`; each agent explicitly assigns allowed names with `sandboxes=[...]` for authored tools to access through `context.sandboxes`. No automatic model tools. Root names are available to same-project subagents; child-local names cannot duplicate ancestors. |
| `skills/<skill>/SKILL.md` | Progressive internal instructions. Frontmatter `name` matches `<skill>`; references, assets, and scripts may live below it. |
| root `evals/<id>.evalset.json` | ADK `EvalSet` whose ID matches the filename. Optional root `evals/test_config.json` configures evaluation. |
| `tests/unit/test_*.py` | Offline authored tests. |
| `tests/smoke/test_*.py` | Explicitly enabled live model, MCP, or HTTP tests. |

`extensions/<name>/extension.yaml` declares a Harnest Extension that owns
same-process Python behavior and declared content. An **Agent Plugin** under
`plugins/` declares `plugin.json`, with optional `skills/` and `mcp.json`. Either
component can stand alone. Unsupported client namespaces are ignored.

Use the canonical schemas at `https://agent-plugins.org/schemas/1.0.0/plugin.schema.json`
and `https://agent-plugins.org/schemas/1.0.0/mcp.schema.json`. For stdio, specify
one bare executable or `./` package-relative command. Only `${PLUGIN_ROOT}` and
`${PLUGIN_DATA}` expand in arguments, environment values and `cwd`; other
placeholders remain literal. Never embed credentials. HTTP URLs and headers
are literal and non-loopback servers require HTTPS. Harnest owns per-installation
persistent `PLUGIN_DATA`; `HARNEST_PLUGIN_DATA_DIR` selects its parent directory.

Root hooks and factories belong in `lifecycle/`; extension-owned hooks belong
in `extensions/<name>/lifecycle/`. Preview `harnest upgrade` to migrate legacy
root `extensions/` and RuntimePlugin packages safely, with backups and collision
checks. Do not mix old lifecycle files and new packages under `extensions/`.

## Harnest Extension descriptor and export

Keep the descriptor closed and explicit:

```yaml
apiVersion: harnest.dev/v1alpha1
kind: Extension
metadata:
  name: temporal
  version: 1.0.0
runtime:
  entrypoint: extension:extension
requires:
  extensions: [core]
capabilities:
  - lifecycle.tool
  - lifecycle.skills
  - context.continuations
  - context.skills
  - context.storage
```

`requires.extensions` names local Harnest Extensions. Harnest orders dependencies
before dependants for startup and reverses that order for shutdown; missing
dependencies and cycles fail compilation. `capabilities` declares the bounded
Harnest lifecycle, context, content, storage, HTTP, native, policy, or telemetry
surfaces the plugin contributes. Declare only surfaces the plugin actually
uses; declaration does not grant access outside Harnest-owned boundaries.
`context.continuations` is for Harnest Extensions that adapt an external durable
runtime. It requires a Harnest-owned portable checkpoint store and does not
make the provider's job identifiers public.

```python
from harnest.extensions import Extension


class Temporal(Extension):
    pass


extension = Temporal()
```

Both the public class and singleton must be local exports of `extension.py`.
Consumers import them from `harnest.extensions.temporal`. A plugin may extend
`ExtensionContext`; its singleton's `.context` is invocation-scoped and revoked
after the call. Application hooks `start(context)` and `stop()` are async.

Harnest Extensions install no private environment and share the root Python environment.
Agent Plugin MCP servers use separate stdio processes or remote transports. A Harnest Extension
may own a PEP 621 `pyproject.toml` when packaging its SDK-facing dependencies;
Harnest resolves those constraints jointly with the root project before any
plugin or agent import. The plugin project name and version must match
`extension.yaml`, dependencies must be static, and compiler-owned Harnest or
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
- Harnest Extensions, agent-plugins, and root lifecycle are root-only; nested
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
  guides. In managed mode, `--example` adds ignored `_example.py` templates
  only to folders without default code, plus ignored native-format skill,
  plugin, and eval samples. Existing agent and storage code remain unchanged.
- Once a public resource exists, the full convention is strict.
- Resource discovery is deterministic by path name.
- Duplicate tool, MCP configuration, subagent, or skill identities fail. MCP
  configuration equality ignores compiler identity and approval metadata.
- MCP capabilities receive deterministic path-scoped runtime identities, so
  same-named direct, agent-plugin, Harnest Extension, and subagent clients remain
  distinct.
- Root and Harnest Extension extensions are flattened in dependency order, then
  validated as one application. Duplicate singleton authorities, named
  resources, routes, native integrations, and context values fail globally.
- Library modules are copied and importable, but their callables are never
  discovered or injected into an agent.
- Public symlinks are rejected so compiled artifacts remain self-contained.
- Compilation validates the optional `config.yaml` root `server` section and
  emits mutable `server.yaml` beside `harnest-agent`. Legacy authored
  `server.yaml` remains supported; declaring both forms is an error. Exact
  `${NAME}` values are preserved and resolved only when the launcher starts.
  The authored config stays hashed under `source/`; omit all server settings
  for defaults. Never place authentication, TLS, secrets, or storage in `server`.
- Do not edit or commit `.harnest/`; it is disposable compiler output.
- Runtime `skills/` are not the same as `.agents/skills/harnest-authoring/`,
  which teaches a coding agent how to modify this project.
