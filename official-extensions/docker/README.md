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
from harnest.extensions.docker import docker, DockerScope
from harnest.sandbox import SandboxNetworkPolicy


python = docker.sandbox(
    image="python:3.12-slim@sha256:<approved-digest>",
    network_policy=SandboxNetworkPolicy.none(),
    scope=DockerScope.EXECUTION,
)
```

Add `"python"` to the consuming agent's `sandboxes=[...]` grant, then invoke it
from an authored tool through `context.sandboxes["python"]`. Exactly one of
`image` or `docker_path` is required. A pinned image digest is recommended for
reproducible deployments.

## Compatibility and capabilities

Version 0.3.0 requires Python 3.10 or newer, Harnest `>=0.15,<0.16`, and the
Docker Python SDK `>=7.1,<8`. The host must provide a reachable Docker daemon;
installing this wheel does not install or start Docker.

The extension provides lazy container startup, fresh execution-scoped
containers, identity-bound invocation or session reuse, resource budgets,
deadlines, bounded output, cleanup, and supported ADK parsing/retry options. It
supports no-network and unrestricted Docker network modes. SDK transport
timeouts for image and container startup are constrained by the remaining
deadline. A host watchdog and control checks enforce the execution deadline.
Cleanup receives a separate bounded five-second window. Daemon
errors and startup timeouts identify the failed phase without exposing Docker
SDK details, image-defined health checks are disabled, and managed containers
carry `dev.harnest.*` labels for operator inventory.

## Multi-container topologies

Declare bounded services when submitted Python must reach another container:

```python
from harnest.extensions.docker import docker, DockerScope
from harnest.sandbox import SandboxBudget, SandboxNetworkPolicy


queue = docker.service(
    name="queue",
    image="redis:7-alpine@sha256:<approved-digest>",
    command=["redis-server", "--save", ""],
    ports=[6379],
    readiness=docker.readiness(command=["redis-cli", "ping"]),
    budget=SandboxBudget(memory_bytes=128 * 1024 * 1024),
)

python = docker.sandbox(
    image="python:3.12-slim@sha256:<approved-digest>",
    services=[queue],
    network=docker.network(internal=True),
    network_policy=SandboxNetworkPolicy.none(),
    scope=DockerScope.SESSION,
)
```

The extension creates one uniquely named bridge for the primary container and
up to eight services. Service names and aliases resolve through internal Docker
DNS. Declared ports are container-to-container documentation; the extension
never publishes them on the host. An internal network permits only topology
traffic even when the portable policy denies external egress. Set
`internal=False` only with `SandboxNetworkPolicy.unrestricted()`.

Services start in declaration order and must become healthy before submitted
Python runs. Each service receives its own `SandboxBudget`. Retained invocation
or session scopes keep their services running while the primary execution
container is quiescent. A stopped or unhealthy service poisons the topology.
Cleanup removes the primary container, services in reverse creation order, and
then the owned network; uncertain cleanup blocks replacement for a later retry.

Startup, service, execution, and cleanup lifecycle events emit correlated logs
and traces through Harnest observability. Every signal carries
`harnest.extension.name=docker`; topology IDs and service names support resource
correlation without recording image references, commands, environment values,
user/session identities, or Docker error messages.

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
