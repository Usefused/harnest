# Chrome sandbox agent

This agent uses an authored `browse_page` tool to open approved sites in a
headless Chromium browser. The browser code runs in a fresh Harnest Docker
sandbox; the model never receives a general code execution tool.

## Requirements

- Harnest installed on macOS or Linux. Run `harnest --version` and
  `harnest doctor` to verify the CLI and managed runtime.
- Docker running Linux containers and reachable from the shell. Confirm with
  `docker version`.
- Outbound access to `mcr.microsoft.com` and Python's package index while
  building the image, plus access to each site you add to `ALLOWED_HOSTS` while
  the agent runs.
- About 5 GB of free Docker storage for the Playwright image and build layers.
- Capacity for the sandbox's one CPU, 1 GiB memory, 128 processes, and 256 MiB
  scratch-space limits.
- An OpenAI API key. The example's `config.yaml` selects `gpt-4.1-mini` through
  `https://api.openai.com/v1`.

You do not need to install Python or Playwright locally. `harnest env sync`
creates the isolated agent environment and installs Harnest's managed framework
runtime. The agent installs `harnest-extension-docker` from PyPI into its local
`extensions/docker/` package; core Harnest does not bundle the Docker SDK. The
Dockerfile installs Playwright and Chromium in the sandbox image, so the root
`pyproject.toml` has no browser dependency.

Install Harnest if `harnest --version` is unavailable:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/Usefused/harnest/main/install.sh |
  sh

harnest --version
harnest doctor
```

## Set up the environment

Run these commands from the Harnest repository root:

```bash
export OPENAI_API_KEY="your-api-key"

docker version
docker build \
  -t harnest-chrome-sandbox:1.61.0 \
  examples/chrome-sandbox/sandbox/_chrome_image

harnest extensions install harnest-extension-docker \
  --project examples/chrome-sandbox
harnest env sync examples/chrome-sandbox
harnest test examples/chrome-sandbox
```

Keep `OPENAI_API_KEY` in the same shell when you serve the agent. Do not add the
key to `config.yaml`, source files, or the image.

## Run it

```bash
harnest serve examples/chrome-sandbox
```

Open `http://127.0.0.1:8080/` and ask the agent: “Read
https://example.com and tell me what it is for.”

The Playwright image is large, so its first build can take a few minutes. Add
trusted hosts to `ALLOWED_HOSTS` in `tools/browse_page.py` before using other
sites. Page text is untrusted data and must never override the agent's
instructions.

The container has outbound network access because a browser needs it. Harnest
still runs it as a non-root user with a read-only root filesystem, dropped
capabilities, bounded resources, and a fresh container for every call.
