"""Framework-independent Docker execution with host-owned transport and cleanup."""

from __future__ import annotations

import atexit
from typing import Any

from .sandbox_guard import GuardedContainer, close_guarded_executor
from .sandbox_startup import OwnedStartupClient, check_startup
from .sandbox_types import SandboxRequest, SandboxResult


class DockerExecutor:
    """Own exactly one Docker container, without importing a framework executor."""

    native_timeout_exit_code = None

    def __init__(self, config: dict[str, Any], output_limit: int) -> None:
        """Capture partial startup ownership so a failed start cannot leak its container."""
        self.timeout_seconds = config["timeout_seconds"]
        self._client = self._container = self._guard = None
        self._guard_poisoned = self._startup_uncertain = False
        try:
            self._start(config, output_limit)
        except BaseException:
            close_guarded_executor(self)
            raise
        atexit.register(self.close)

    def _start(self, config: dict[str, Any], output_limit: int) -> None:
        """Start with kernel budgets before running even the Python availability probe."""
        import docker

        check_startup()
        client = docker.DockerClient(base_url=config["base_url"]) if config["base_url"] else docker.from_env()
        self._client = OwnedStartupClient(self, client, limits=config["harnest_resource_limits"])
        image = self._image(config)
        check_startup()
        self._container = self._client.containers.run(
            image=image, detach=True, tty=False,
            entrypoint=["python3", "-c", "import time; time.sleep(100000000)"],
            network_mode="bridge" if config["network_enabled"] else "none",
        )
        self._guard = GuardedContainer(self, self._container, output_limit)
        self._container = self._guard
        check_startup()
        result = self._guard.exec_run(["python3", "--version"])
        if result.exit_code != 0:
            raise RuntimeError("sandbox image requires python3")

    def _image(self, config: dict[str, Any]) -> str:
        """Use the exact built image ID rather than a shared mutable build tag."""
        from docker.errors import ImageNotFound

        if config["docker_path"] is not None:
            image, _logs = self._client.images.build(path=config["docker_path"], rm=True)
        else:
            try:
                image = self._client.images.get(config["image"])
            except ImageNotFound:
                image = self._client.images.pull(config["image"])
        # Dockerfile VOLUME directives would create writable, unbudgeted disks.
        if image.attrs.get("Config", {}).get("Volumes"):
            raise ValueError("sandbox images must not declare volumes; use budgeted /tmp scratch")
        return image.id

    def execute(self, request: SandboxRequest) -> SandboxResult:
        """Keep the deadline outside the executed Python process and preserve exit status."""
        result = self._guard.exec_run(["python3", "-c", request.code], demux=True)
        stdout, stderr = result.output
        return SandboxResult(
            stdout=(stdout or b"").decode("utf-8", errors="replace"),
            stderr=(stderr or b"").decode("utf-8", errors="replace"),
            status=self._guard.status, exit_code=self._guard.exit_code,
        )

    def close(self) -> None:
        """Retain failed cleanup ownership and unregister successful lifecycle cleanup."""
        close_guarded_executor(self)


def create_docker_executor(config: dict[str, Any], output_limit: int) -> DockerExecutor:
    """Expose a testable construction boundary without importing ADK or LangGraph."""
    return DockerExecutor(config, output_limit)
