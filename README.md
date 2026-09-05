# Harnest by [Fused](https://usefused.com)

Harnest is a production application harness for ADK and LangGraph agents.

It manages the work around your agent:
project structure, dependencies, capability discovery, compilation, tests,
serving, sessions, approvals, authentication, storage, telemetry, and a
development playground.

Every Harnest agent is compiled from the capabilities you add. Tools, subagents,
MCP connections, agent skills, Agent Plugins, Harnest Extensions, sandboxes, and
lifecycle hooks are left out unless you use them.

| Mode | Harnest manages | You control |
| --- | --- | --- |
| Managed | Discovery, wiring, framework adapters, tests, and runtime services | Portable agent behavior |
| Advanced | Packaging, tests, serving, sessions, auth, and telemetry | The underlying framework directly |

Harnest builds on the work of [Google ADK](https://google.github.io/adk-docs/)
and [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview). Start
in managed mode for the fastest path, or use advanced mode when you need direct
framework control. Existing agents can start in advanced mode and move
capabilities into the managed structure at their own pace.

## Install

Install the latest macOS or Linux release:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/Usefused/harnest/main/install.sh |
  sh
```

The release contains the native CLI, its matching Python runtime package, and
the `uv` bootstrapper. It does not require a preinstalled Python. The installer
shows the selected version and paths before asking for confirmation.

Verify the installation:

```bash
harnest --version
harnest doctor
```

See [Installation and releases](https://docs.usefused.com/harnest/reference/installation-and-releases)
for version pinning, non-interactive installation, upgrades, checksums, and
private forks.

## Initialize a project

Create an ADK or LangGraph agent:

```bash
harnest init support-agent --framework adk
# or
harnest init support-agent --framework langgraph
```

The default scaffold is a small, runnable managed agent. Add `--example` for
ignored code samples in folders that otherwise contain only guides. Existing
agent and storage code stays unchanged; rename a sample to opt into a feature:

```bash
harnest init support-agent --framework adk --example
```

Synchronize the isolated project environment and run its offline tests:

```bash
harnest env sync support-agent
harnest test support-agent
```

`compile`, `test`, and `serve` also synchronize the environment automatically.
The explicit `env sync` command also maintains an IDE-detectable `.venv` link
unless that path already belongs to the user.
Add only agent-owned provider, tool, and library packages to the generated
`pyproject.toml`; Harnest owns the selected framework dependency.

## Migrate a project

### Bring an existing agent into Harnest

Choose managed mode for portable agent behavior. If the agent depends on native
plugins, middleware, state, or framework APIs, start in advanced mode so it
keeps direct framework control.

```bash
harnest init migrated-agent --framework adk
# or preserve native control while adopting the harness
harnest init migrated-agent --framework adk --mode advanced
```

Move the agent into the new project, then run `harnest test` and `harnest
serve`. Advanced mode keeps Harnest's packaging, testing, server, sessions,
authentication, storage, telemetry, and playground. Move compatible
capabilities into the managed structure when useful.

### Upgrade an older Harnest project

Preview the repository migration first. This command is read-only:

```bash
harnest upgrade existing-agent
```

After reviewing the plan and preserving the current work, apply it:

```bash
harnest upgrade existing-agent --apply
harnest test existing-agent
```

Harnest verifies the planned source hashes and backs up affected files under
`existing-agent/.harnest/upgrade-backups/` before changing them. It reports
ambiguous business logic as a manual blocker instead of guessing.

### Switch between ADK and LangGraph

For a managed agent, change `spec.framework.name` in `config.yaml`:

```yaml
spec:
  framework:
    name: langgraph # or adk
    mode: managed
```

Then validate the target framework:

```bash
harnest test existing-agent
harnest serve existing-agent
```

Before switching, review native extensions, ADK eval sets, sandboxes, custom
nodes, and framework-owned checkpoint state. Advanced projects use framework
APIs directly and require a semantic migration rather than only a config edit.
Follow the [framework migration checklist](https://docs.usefused.com/harnest/runtime/adk-and-langgraph#switch-frameworks).

## Serve a project

Compile and start the standalone development server:

```bash
harnest serve support-agent
```

During development, recompile and replace the local process after source changes:

```bash
harnest serve support-agent --reload
```

Reload uses fresh immutable artifacts and never mutates a running ADK or LangGraph graph. It is restricted to loopback development serving.

Open [http://127.0.0.1:8080/](http://127.0.0.1:8080/) for the built-in test UI.
The same playground works with managed or advanced ADK and LangGraph agents.
The neutral API is documented at
[http://127.0.0.1:8080/docs](http://127.0.0.1:8080/docs).

Configure the local bind, request limits, concurrency, timeout, and playground
in the optional `server:` section of `config.yaml`. Omit it to use the defaults. Set `server.live: true` to enable WebSockets on the same host and port. See [Serving agents](https://docs.usefused.com/harnest/runtime/serving)
for the HTTP, SSE, WebSocket, approval, authentication, storage, and production
boundaries.

Portable image, audio, video, file, and typed custom-data fields are declared
in Pydantic models with reusable `Annotated` constraints. See [Typed
multimodal contracts](https://docs.usefused.com/harnest/build/models-and-libraries/typed-multimodal-contracts).

## Documentation

Browse the [Harnest documentation](https://docs.usefused.com/harnest)
for Agent Skills, MCP Client, SubAgents, Agent Tools, Lifecycle, authentication
and credentials, telemetry, frameworks, testing, serving, and architecture.

Harnest is licensed under the [Apache License 2.0](LICENSE).
