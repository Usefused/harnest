"""Run browser work through the application-installed Docker extension."""

from harnest.extensions.docker import docker
from harnest.sandbox import SandboxBudget, SandboxNetworkPolicy


chrome = docker.sandbox(
    image="harnest-chrome-sandbox:1.61.0",
    network_policy=SandboxNetworkPolicy.unrestricted(),
    timeout_seconds=45,
    max_output_bytes=32_768,
    budget=SandboxBudget(
        cpu=1.0,
        memory_bytes=1024 * 1024 * 1024,
        pids=128,
        scratch_bytes=256 * 1024 * 1024,
    ),
)
