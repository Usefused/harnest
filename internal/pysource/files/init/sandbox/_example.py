"""Rename to calculations.py, then assign sandboxes=["calculations"] on Agent.

From an authored tool, use context.sandboxes["calculations"].execute(code)
or await its aexecute(code). Import context from harnest; check result.stderr
and choose which output to return. No model tool is created automatically.

Install first with harnest extensions install docker --project <agent-root>,
then run harnest env sync <agent-root>. Docker is required only on execution;
the extension owns its isolation and resource enforcement.
"""

from harnest.extensions.docker import docker
from harnest.sandbox import SandboxNetworkPolicy


calculations = docker.sandbox(
    image="python:3.12-slim",
    network_policy=SandboxNetworkPolicy.none(),
    timeout_seconds=120,
    max_output_bytes=1_048_576,
)
