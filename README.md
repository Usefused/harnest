# Harnest by [Fused](https://usefused.com)

Harnest is an agent harness for teams moving beyond “the model responded” to
“this needs to work in production.”

It handles the day-two work around ADK and LangGraph agents: repeatable builds
and tests, serving, authentication, approvals, persistent sessions, durable
background tasks, and telemetry. You own the agent’s behavior; Harnest provides
the structure and runtime around it.

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

The runtime includes the `harnest_postgres` and `harnest_redis` storage packages;
no separate provider-package installation is required.

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

New agents use the OpenAI-compatible API specification, not a default provider
or GPT model. Replace `OPENAI_MODEL` and `OPENAI_BASE_URL` placeholders in
`config.yaml` with your server's model ID and API URL. Set `OPENAI_API_KEY` in
your runtime environment only if that server requires authentication. See
[Configure a model](https://docs.usefused.com/harnest/build/models-and-libraries/configure-a-model).

The default scaffold creates this agent folder. Files beginning with `_` are
ignored guides; replace only the ones for capabilities you need.

```text
support-agent/
├── agent.py
├── instructions.md
├── config.yaml
├── agent-card.yaml
├── pyproject.toml
├── harnest.lock
├── .gitignore
├── lifecycle/
│   ├── storage.py
│   └── _README.md
├── lib/
│   └── _README.md
├── models/
│   └── _README.md
├── tools/
│   └── _README.md
├── tasks/
│   └── _README.md
├── cron/
│   └── _README.md
├── subagents/
│   └── _README.md
├── mcp/
│   └── _README.md
├── extensions/
│   └── _README.md
├── plugins/
│   └── _README.md
├── sandbox/
│   └── _README.md
├── skills/
│   └── _README.md
├── evals/
│   └── _README.md
└── tests/
    ├── unit/_README.md
    └── smoke/_README.md
```

Choose a scaffold profile:

| Profile | Result |
| --- | --- |
| Default | Runnable managed agent plus ignored guides for optional capabilities |
| `--minimal` | Only the files required to compile and run |
| `--example` | Default scaffold plus ignored, opt-in examples |

Start with only the runnable core, then add capabilities as needed:

```bash
harnest init minimal-agent --framework adk --minimal
cd minimal-agent
harnest add tool customer-lookup
harnest add subagent researcher
```

`harnest add` also scaffolds `task`, `lifecycle`, and `context` resources without
overwriting existing files. When running outside the agent folder, pass
`--project <agent-root>`.

Synchronize the isolated project environment and run its offline tests:

```bash
cd support-agent
harnest env sync .
harnest env sync . --profile development
harnest test .
```

`compile` alone selects the lean production runtime profile. `serve`, `run`,
and ordinary `test` share the development profile; `test --evals` selects the
eval profile. Each command synchronizes its environment automatically.
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
cd existing-agent
harnest upgrade .
```

After reviewing the plan and preserving the current work, apply it:

```bash
harnest upgrade . --apply
harnest test .
```

Harnest verifies the planned source hashes and backs up affected files under
`.harnest/upgrade-backups/` before changing them. It reports
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
harnest test .
harnest serve .
```

Before switching, review native extensions, ADK eval sets, sandboxes, custom
nodes, and framework-owned checkpoint state. Advanced projects use framework
APIs directly and require a semantic migration rather than only a config edit.
Follow the [framework migration checklist](https://docs.usefused.com/harnest/runtime/adk-and-langgraph#switch-frameworks).

## Serve a project

From the agent folder, compile and start the standalone development server:

```bash
harnest serve .
```

During development, recompile and replace the local process after source changes:

```bash
harnest serve . --reload
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
