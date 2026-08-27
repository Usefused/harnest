# Sandboxes

Sandboxes provide the ADK code executor used for model-generated code. They are
agent-wide and optional. Configure one file at `sandbox/sandbox.py`, exporting a
value named `sandbox`:

```python
from harnest.sandbox import Sandbox


sandbox = Sandbox.container(
    image="acme/harnest-python-sandbox:2026-08",
    network=False,
    timeout_seconds=120,
)
```

The container backend uses ADK's `ContainerCodeExecutor`, disables networking
by default, and requires Docker plus `google-adk[extensions]` in the agent's
`requirements.txt`. An image or `docker_path` is required; Harnest does not
silently run model-generated code in the agent server process.

Third-party providers integrate without changing Harnest:

```python
from harnest.sandbox import Sandbox
from company_sandbox import CompanyExecutor


sandbox = Sandbox.provider(
    lambda: CompanyExecutor(pool="agents"),
    name="company-sandbox",
    timeout_seconds=120,
)
```

The provider factory is lazy. Compiling and starting an agent creates an ADK
proxy executor, while the real Docker or remote backend is created only when
code execution is first requested. The returned value must be an ADK
`BaseCodeExecutor`; otherwise execution fails with an explicit type error.

Harnest validates the folder contract, rejects symlinks, rejects additional
public sandbox files, and prevents configuring both `Agent(sandbox=...)` and
`sandbox/sandbox.py`. It does not claim that a custom provider is isolated. The
provider owns filesystem, network, process, tenancy, cleanup, and credential
guarantees. Likewise, `config.yaml` permissions remain deployment policy and
are not a substitute for an enforcing sandbox backend.
