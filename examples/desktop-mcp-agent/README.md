# Linux desktop agent with Chrome and MCP

This Harnest example runs an ADK agent with one Linux desktop of its own. The
desktop starts with the agent in Docker and stays alive for the agent's lifetime.
Chrome runs on a virtual X11 display, so no physical monitor is needed. The
agent uses Playwright for navigation and desktop tools for screenshots, clicks,
typing, and keys. Jev decides whether each request should use those tools. A
separate Streamable HTTP MCP server exposes the running agent through `ask_agent`.
Jev sees the two previous user requests from the same session so brief follow-ups,
such as new dates for a flight search, keep their context.

## What you need

- Docker with its daemon running and enough space to build the Playwright/Chrome
  image. The image runs as `linux/amd64`, including on Apple Silicon hosts.
- Python 3.12, `uv`, and Go 1.24 or newer when running from this source checkout.
- A DeepSeek API key in `DEEPSEEK_API_KEY` and a Jev key in `TYPESAFE_AI_KEY`.
  The keys are read from your shell environment; do not put them in `config.yaml`.
- Network access on first run to fetch Python and Go dependencies and build the
  Docker image. The agent also needs access to its model and Jev endpoints.

From the repository root, prepare the source checkout once:

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[all]'
```

If you already have a working root `.venv` with Harnest and ADK installed, keep
it. The provisioner installs this example's extra dependencies into that
environment. It uses `go run ./cmd/harnest` so the CLI matches this checkout.

## Start an agent

Export your keys in the shell that will run the provisioner, then choose a
unique lowercase name:

```sh
export DEEPSEEK_API_KEY='your-deepseek-key'
export TYPESAFE_AI_KEY='your-jev-key'
./examples/desktop-mcp-agent/provision.sh travel
```

The first run builds the desktop image and may take several minutes. Startup
waits for Chrome and the MCP server, then prints URLs like these:

```text
Agent travel: http://127.0.0.1:49100/
MCP:          http://127.0.0.1:49101/mcp
```

Open the agent URL for the Harnest Playground. Configure an MCP client to use
the printed `/mcp` URL with **Streamable HTTP** transport. Its `ask_agent` tool
accepts `prompt` and an optional `session_id`; pass the returned `session_id`
on later calls to continue the conversation. The URLs and ports are chosen at
startup, so use the values printed for your instance.

The command stays running while the agent, MCP server, and desktop are in use.
Press Ctrl+C in that terminal to stop this agent and remove its desktop
container. Run the command again with a different name to provision another
independent agent and desktop:

```sh
./examples/desktop-mcp-agent/provision.sh research
```

## Options

Add `--viewer` to print a local noVNC URL where you can watch and interact with
the desktop. Chrome still runs on the virtual display without this option:

```sh
./examples/desktop-mcp-agent/provision.sh travel --viewer
```

The launcher defaults to `deepseek-flash` at `https://api.deepseek.com`. It
always uses `DEEPSEEK_API_KEY` for that endpoint, even if `OPENAI_API_KEY` is
also set; you do not need to set `OPENAI_API_KEY` for DeepSeek. To use another
OpenAI-compatible model, set `OPENAI_MODEL`,
`OPENAI_BASE_URL`, and that provider's `OPENAI_API_KEY` before provisioning.
The agent code itself stays provider-neutral.

Browser navigation accepts HTTP(S) URLs by default. To restrict it, set
`DESKTOP_ALLOWED_HOSTS` to a comma-separated list of exact hostnames before
starting the agent, for example `example.com,www.example.com`. Redirects and
page resources must also use listed hosts. The agent, MCP, desktop control API,
and optional viewer bind to loopback addresses on the host.
