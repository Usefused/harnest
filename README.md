# Harnest by [Fused](https://usefused.com)

**Ship your next agent. Set the standard for every one after it.**

Harnest is a production harness for Google ADK and LangGraph agents. It gives you a shared structure for building, testing, packaging, and serving agents. Focus on the workflows your agent needs to deliver.

**[Explore an interactive Harnest project →](https://usefused.com/docs/harnest/overview#explore-a-harnest-project)**

Click through the project’s files, see real code, and discover where tools, skills, MCP connections, and runtime capabilities fit.

- **For teams:** turn company defaults into reusable project packs. Give every team a consistent starting point, your own CLI, and versioned project upgrades.
- **For individual developers:** start with a small agent, connect your tools and model, and use the same workflow to test, build, and serve it.

[Build your first agent](https://usefused.com/docs/harnest/get-started/install) · [Create your company standard](https://usefused.com/docs/harnest/build/project-packs)

## Install

Install the CLI on macOS or Linux. It does not require a preinstalled Python.

```bash
curl -fsSL \
  https://raw.githubusercontent.com/Usefused/harnest/main/install.sh |
  sh
harnest doctor
```

See [installation options](https://usefused.com/docs/harnest/reference/installation-and-releases) for version pinning and CI.

## Build your first agent

```bash
harnest init support-agent --framework adk
cd support-agent
```

Prefer LangGraph? Use `--framework langgraph` instead.

Set `OPENAI_MODEL` and `OPENAI_BASE_URL` in `config.yaml` to your OpenAI-compatible model and endpoint. Supply `OPENAI_API_KEY` through the runtime environment if your provider requires it. See [Configure a model](https://usefused.com/docs/harnest/build/models-and-libraries/configure-a-model).

Write the agent’s instructions in `instructions.md` and add the capabilities your use case needs. Harnest discovers and wires managed tools, skills, and MCP connections from the project structure.

```bash
harnest test .
harnest serve . --reload
```

Open the [local playground](http://127.0.0.1:1907/) to try your agent. These commands prepare the project environment automatically.

Prefer a visual workspace? Run `harnest studio --workspace /path/to/agents` to build and edit agents in Harnest Studio.

## Bring your existing agents

Use **managed mode** for portable agent behavior, or **advanced mode** to keep direct control of ADK or LangGraph while adopting Harnest’s packaging, tests, and serving.

```bash
harnest init migrated-agent --framework adk --mode advanced
```

Follow the [migration guide](https://usefused.com/docs/harnest/get-started/migrate) to move your code. For an existing Harnest project, preview changes with `harnest upgrade .` before applying them with `harnest upgrade . --apply`. See the [framework migration checklist](https://usefused.com/docs/harnest/runtime/adk-and-langgraph#switch-frameworks) when changing frameworks.

## Take it to production

Keep the same agent code as you add persistent storage, authentication, approvals, and telemetry. Harnest provides the runtime interfaces; you configure the services and deployment your application needs.

[Prepare for production](https://usefused.com/docs/harnest/runtime/serving/production) · [Read the documentation](https://usefused.com/docs/harnest)

Built and maintained by [Fused](https://usefused.com). Licensed under [Apache 2.0](LICENSE).
