# Harnest Docker Extension

The official Docker sandbox provider for [Harnest](https://github.com/Usefused/harnest),
built and maintained by Fused. It lets a Harnest application run
framework-neutral Python sandbox workloads in Docker while keeping the Docker
implementation outside Harnest core.

This is a **Harnest Extension**, loaded into an agent process from its
`extensions/` directory. It is not an Agent Plugin: Agent Plugins contribute
client-facing MCP tools, skills, and UI metadata, while this package extends
Harnest's same-process runtime and declares the privileged `sandbox.provider`
capability.

## Install

Install the published package by short slug or full PyPI project name, then
refresh the agent's locked environment:

```bash
harnest extensions install docker --project ./my-agent
# Equivalent: harnest extensions install harnest-extension-docker --project ./my-agent
harnest env sync ./my-agent
```

To review and install a local checkout instead:

```bash
harnest extensions install ./official-extensions/docker --project ./my-agent
harnest env sync ./my-agent
```

Harnest validates and copies the package without importing its code. Create
`sandbox/python.py` and import the installed extension through its application
namespace. The exported variable must match the filename:

```python
from harnest.extensions.docker import docker
from harnest.sandbox import SandboxNetworkPolicy


python = docker.sandbox(
    image="python:3.12-slim@sha256:<approved-digest>",
    network_policy=SandboxNetworkPolicy.none(),
)
```

Add `"python"` to the consuming agent's `sandboxes=[...]` grant, then invoke it
from an authored tool through `context.sandboxes["python"]`. Exactly one of
`image` or `docker_path` is required. A pinned image digest is recommended for
reproducible deployments.

## Compatibility and capabilities

Version 0.2.0 requires Python 3.10 or newer, Harnest `>=0.14,<0.15`, and the
Docker Python SDK `>=7.1,<8`. The host must provide a reachable Docker daemon;
installing this wheel does not install or start Docker.

The extension provides lazy container startup, fresh execution-scoped
containers, identity-bound invocation or session reuse, resource budgets,
deadlines, bounded output, cleanup, and supported ADK parsing/retry options. It
supports no-network and unrestricted Docker network modes. Daemon calls inherit
the remaining all-in sandbox deadline across admission, image/container startup,
and execution. Cleanup receives a separate bounded five-second window. Daemon
errors and startup timeouts identify the failed phase without exposing Docker
SDK details, image-defined health checks are disabled, and managed containers
carry `dev.harnest.*` labels for operator inventory.

## Security and limitations

Docker daemon access is highly privileged. Protect its socket or remote API,
restrict who can configure the extension, and use trusted, digest-pinned images.
The extension rejects network-policy substitution and fails closed for exact
host/port allowlists and unrestricted mode with private-network blocking because
those controls are not yet enforced by this provider. It is a Python execution
sandbox, not a browser tool, model provider, or general container orchestrator.

See the [Harnest documentation](https://docs.usefused.com/harnest) for agent
configuration and operational guidance. Source and issue tracking live in the
[Harnest repository](https://github.com/Usefused/harnest).
