# Official Harnest Extensions

These directories are Harnest Extensions built and maintained by Fused. They
are source packages rather than complete agents. Install a reviewed directory
with `harnest extensions install SOURCE --project AGENT_DIR`, then run `harnest
env sync AGENT_DIR`. Each extension owns PEP 621 metadata whose SDK dependencies
join the agent's root environment solve and committed runtime lock.

`hatchet/` demonstrates an agentic adapter for an independently deployed
Hatchet runtime. It intentionally contributes no Harnest tools: the consuming
agent defines its own domain tool and calls `harnest.extensions.hatchet.hatchet`.
Install it with:

```bash
harnest extensions install official-extensions/hatchet --project my-agent
harnest env sync my-agent
```

`docker/` is the official application-local Docker sandbox provider. Docker is
not part of Harnest core: install the extension into `extensions/docker/`, then
create a named sandbox such as
`sandbox/python.py`:

```bash
harnest extensions install docker --project my-agent
harnest env sync my-agent
```

```python
from harnest.extensions.docker import docker
from harnest.sandbox import SandboxNetworkPolicy


python = docker.sandbox(
    image="python:3.12-slim@sha256:<approved-digest>",
    network_policy=SandboxNetworkPolicy.none(),
)
```

The extension exposes a framework-neutral `Sandbox` factory, declares
`sandbox.provider`, and supports generic Python workloads without adding a
model or browser tool. The consuming agent explicitly grants `"python"` in its
`sandboxes` list; an authored tool invokes it through
`context.sandboxes["python"]`.

The extension owns the Docker SDK and container implementation while preserving
identity scopes, budgets, deadlines, output limits, cleanup, and ADK
parsing/retry options behind Harnest's provider-neutral sandbox contract. It
supports provider-enforced `none` and `unrestricted` network modes. Exact
host/port allowlists and unrestricted mode with private-network blocking fail
closed until the extension implements those controls. Exactly one of `image` or
`docker_path` is required.

The live infrastructure fixture is under `tests/fixtures/hatchet/`. It keeps
Hatchet and its worker in Docker so stopping Harnest cannot stop an external
job.

## Publishing

`.github/workflows/publish-extensions.yml` builds and publishes each official
extension through PyPI Trusted Publishing. It does not use a PyPI API token,
pins every third-party action to a reviewed commit, and rejects release tags
whose commit is not reachable from `main`.
Configure each project's trusted publisher with these values:

- owner: `Usefused`;
- repository: `harnest`;
- workflow: `publish-extensions.yml`; and
- environment: `pypi`.

The pending publishers must use the exact distribution names
`harnest-extension-docker` and `harnest-extension-hatchet`; the first matching
workflow run creates each project. A maintainer can dispatch **Publish Official
Extensions** from `main`, or push a tag whose version matches both
`pyproject.toml` and `extension.yaml`, for example
`harnest-extension-docker-v0.2.0`. Ordinary extension changes build and verify
both official wheels without publishing them. The `pypi` GitHub environment may
require reviewers before its publish job starts.
