# Harnest Docker Extension

The official Docker sandbox provider for
[Harnest](https://docs.usefused.com/harnest), built and maintained by Fused.
It runs generic Python sandbox workloads through Docker without introducing a
browser or model-tool contract.

This extension requires Harnest 0.14.x. Harnest discovers its distribution
metadata on PyPI. Install a reviewed source checkout into an agent so its
dependency is included in the agent's runtime lock:

```bash
harnest extensions install ./official-extensions/docker --project my-agent
harnest env sync my-agent
```

Sandbox network policy, resource budgets, execution identity, deadlines, output
limits, and cleanup remain enforced by Harnest's sandbox boundary. Consult the
[Harnest documentation](https://docs.usefused.com/harnest) for configuration and
compatibility details.
