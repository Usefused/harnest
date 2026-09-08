# Harnest Hatchet Extension

The official durable-workflow adapter between
[Harnest](https://github.com/Usefused/harnest) and an independently operated
Hatchet runtime, built and maintained by Fused. It submits, inspects, waits for,
and cancels external Hatchet workflow runs while Harnest owns agent execution
and durable continuation state.

This is a **Harnest Extension**, loaded into an agent process from its
`extensions/` directory. It is not an Agent Plugin: Agent Plugins contribute
client-facing MCP tools, skills, and UI metadata. This package contributes no
agent tools; the consuming agent authors domain-specific tools and calls the
same-process `harnest.extensions.hatchet` API.

## Install

Install the published package by short slug or full PyPI project name, then
refresh the agent's locked environment:

```bash
harnest extensions install hatchet --project ./my-agent
# Equivalent: harnest extensions install harnest-extension-hatchet --project ./my-agent
harnest env sync ./my-agent
```

To review and install a local checkout instead:

```bash
harnest extensions install ./official-extensions/hatchet --project ./my-agent
harnest env sync ./my-agent
```

Harnest validates and copies the package without importing its code. A
consumer-owned asynchronous tool can use the installed public API:

```python
from harnest.extensions.hatchet import hatchet
from harnest.agent import tool


@tool(durable=True)
async def build_report(account_id: str) -> dict:
    """Build one account report in the external workflow system."""
    return await hatchet.run_and_wait(
        "build-report", {"account_id": account_id}
    )
```

`hatchet.status(job)` reads current state and `hatchet.cancel(job)` requests
cancellation. Hatchet workers and workflow definitions remain independently
deployed; stopping Harnest does not stop them.

## Compatibility and capabilities

Version 0.1.1 requires Python 3.10 or newer, Harnest `>=0.15,<0.16`, and
`hatchet-sdk>=1.38,<2`. It declares `context.credentials` and
`context.continuations` so calls use invocation-scoped credentials and waits can
survive process restarts. The deployment must supply a Hatchet service
credential in `HATCHET_CLIENT_TOKEN` for startup recovery of pending waits.
The extension reads deployment settings from the process environment or token;
it disables the SDK's implicit working-directory dotenv discovery.

## Security and limitations

Keep Hatchet tokens out of source, job input, continuation results, and logs.
Grant only the required `runs:create`, `runs:read`, or `runs:cancel` scopes
through Harnest's credential resolver. Job inputs and workflow results cross the
external Hatchet boundary. The extension requires JSON mappings and limits each
encoded input or result to 1 MiB before submission or durable persistence.
Recovery uses at most 16 concurrent provider clients, and polling backs off
while retaining pending waits across transient provider outages. The adapter
does not deploy or supervise Hatchet, define workflows, manage workers, expose
tools, or replace durable Harnest session/checkpoint storage. Cancellation is a
provider request and does not imply immediate worker shutdown.

Agent Runtime Principals can cross an external continuation. Harnest persists
only a private versioned snapshot of permission names and reconstructs a fresh
opaque principal when another replica resumes the wait. Principal IDs,
credentials, and authentication claims are never stored with the continuation.

Use `run_and_wait` for work whose result is required because it verifies durable
continuation support before submission. Separate `run` and `wait` calls remain
available for fire-and-forget and existing-run workflows, but callers own the
external job if a later wait fails for another reason.

See the [Harnest documentation](https://docs.usefused.com/harnest) for agent
configuration and operational guidance. Source and issue tracking live in the
[Harnest repository](https://github.com/Usefused/harnest).
