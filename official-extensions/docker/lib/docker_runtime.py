"""Framework-independent Docker execution and lifecycle ownership."""

from __future__ import annotations

import atexit
from typing import Any

from harnest.sandbox import (
    SandboxCancelledError,
    SandboxRequest,
    SandboxResult,
    SandboxStatus,
    control,
)

from .guard import GuardedContainer, close_guarded_executor
from .startup import (
    OwnedStartupClient,
    check_startup,
    constrain_startup_timeout,
)


_MANAGED_LABELS = {
    "dev.harnest.managed": "true",
    "dev.harnest.resource": "sandbox",
}


class DockerStartupError(RuntimeError):
    """Report a sanitized Docker startup phase without retaining SDK details."""

    def __init__(self, phase: str, cause_type: str) -> None:
        """Keep the failed phase and provider exception type as bounded evidence."""

        self.phase = phase
        self.cause_type = cause_type
        super().__init__(
            f"Docker sandbox startup failed during {phase} with {cause_type}"
        )


class DockerExecutor:
    """Own exactly one Docker container without importing an agent framework."""

    native_timeout_exit_code = None

    def __init__(self, config: dict[str, Any], output_limit: int) -> None:
        """Retain partial startup ownership so failures cannot leak containers."""

        self.timeout_seconds = config["timeout_seconds"]
        self._client = None
        self._container = None
        self._guard = None
        self._guard_poisoned = False
        self._startup_uncertain = False
        self._startup_phase = "configuration"
        try:
            self._start(config, output_limit)
        except BaseException as startup_error:
            try:
                close_guarded_executor(self)
            except Exception:
                # The first startup failure remains authoritative, while the
                # exact poisoned owner stays reachable for a later cleanup retry.
                _retain_uncertain_cleanup(startup_error, self)
            raise
        atexit.register(self.close)

    def _start(self, config: dict[str, Any], output_limit: int) -> None:
        """Expose the failed startup phase while sanitizing provider errors."""

        try:
            self._start_owned(config, output_limit)
        except (DockerStartupError, SandboxCancelledError, ValueError):
            raise
        except TimeoutError:
            raise TimeoutError(
                f"Docker sandbox startup timed out during {self._startup_phase}"
            ) from None
        except Exception as error:
            raise DockerStartupError(
                self._startup_phase, type(error).__name__
            ) from None

    def _start_owned(self, config: dict[str, Any], output_limit: int) -> None:
        """Apply kernel budgets before running the Python availability probe."""

        self._startup_phase = "Docker SDK import"
        import docker

        check_startup()
        timeout = config["timeout_seconds"]
        current = control.current()
        remaining = None if current is None else current.remaining()
        if remaining is not None:
            timeout = max(0.001, min(timeout, remaining))
        self._startup_phase = "daemon client initialization"
        client = (
            docker.DockerClient(base_url=config["base_url"], timeout=timeout)
            if config["base_url"]
            else docker.from_env(timeout=timeout)
        )
        self._client = OwnedStartupClient(
            self, client, limits=config["harnest_resource_limits"]
        )
        self._startup_phase = "image resolution"
        image = self._image(config)
        check_startup()
        self._startup_phase = "container creation and start"
        self._container = self._client.containers.run(
            image=image,
            detach=True,
            tty=False,
            entrypoint=["python3", "-c", "import time; time.sleep(100000000)"],
            # Image health checks are autonomous commands outside Harnest's
            # request deadline, output budget, and process-lifecycle control.
            healthcheck={"test": ["NONE"]},
            labels=_MANAGED_LABELS,
            network_mode="bridge" if config["network_enabled"] else "none",
        )
        self._guard = GuardedContainer(self, self._container, output_limit)
        self._container = self._guard
        check_startup()
        self._startup_phase = "Python availability probe"
        result = self._guard.exec_run(["python3", "--version"])
        if self._guard.status is SandboxStatus.TIMED_OUT:
            raise TimeoutError("Docker sandbox Python probe exceeded its deadline")
        if result.exit_code != 0:
            raise DockerStartupError(
                self._startup_phase, "python3 is unavailable"
            )

    def _image(self, config: dict[str, Any]) -> str:
        """Use an immutable built image ID and reject unbudgeted volumes."""

        from docker.errors import ImageNotFound

        constrain_startup_timeout(self._client, self.timeout_seconds)
        if config["docker_path"] is not None:
            self._startup_phase = "image build"
            image, _logs = self._client.images.build(
                path=config["docker_path"], rm=True
            )
        else:
            self._startup_phase = "image lookup"
            try:
                image = self._client.images.get(config["image"])
            except ImageNotFound:
                check_startup()
                constrain_startup_timeout(self._client, self.timeout_seconds)
                self._startup_phase = "image pull"
                image = self._client.images.pull(config["image"])
        check_startup()
        self._startup_phase = "image policy validation"
        if image.attrs.get("Config", {}).get("Volumes"):
            raise ValueError(
                "sandbox images must not declare volumes; use budgeted /tmp scratch"
            )
        return image.id

    def execute(self, request: SandboxRequest) -> SandboxResult:
        """Execute Python with a host deadline and preserve its terminal status."""

        result = self._guard.exec_run(
            ["python3", "-c", request.code], demux=True
        )
        stdout, stderr = result.output
        return SandboxResult(
            stdout=(stdout or b"").decode("utf-8", errors="replace"),
            stderr=(stderr or b"").decode("utf-8", errors="replace"),
            status=self._guard.status,
            exit_code=self._guard.exit_code,
        )

    def close(self) -> None:
        """Retain cleanup ownership until resource removal is confirmed."""

        close_guarded_executor(self)


def create_docker_executor(
    config: dict[str, Any], output_limit: int
) -> DockerExecutor:
    """Construct one Docker executor behind a focused test boundary."""

    return DockerExecutor(config, output_limit)


def _retain_uncertain_cleanup(error: BaseException, executor: DockerExecutor) -> None:
    """Attach cleanup ownership without replacing the primary startup failure."""

    error.failed_executor = executor
    error.cleanup_unconfirmed = True
    message = str(error)
    suffix = "owned-container cleanup could not be confirmed"
    if suffix not in message:
        error.args = (f"{message}; {suffix}",)


__all__ = ["DockerExecutor", "DockerStartupError", "create_docker_executor"]
