"""Framework-independent Docker execution and lifecycle ownership."""

from __future__ import annotations

import atexit
import time
from typing import Any
from uuid import uuid4

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
from .telemetry import docker_operation


_MANAGED_LABELS = {
    "dev.harnest.managed": "true",
    "dev.harnest.resource": "sandbox",
}


def _resource_labels(
    topology_id: str, *, resource: str, service: str | None = None
) -> dict[str, str]:
    """Identify topology resources without persisting invocation identities."""

    labels = {
        "dev.harnest.managed": "true",
        "dev.harnest.resource": resource,
        "dev.harnest.topology": topology_id,
    }
    if service is not None:
        labels["dev.harnest.service"] = service
    return labels


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
    """Own one execution container and its optional private service topology."""

    native_timeout_exit_code = None

    def __init__(self, config: dict[str, Any], output_limit: int) -> None:
        """Retain partial startup ownership so failures cannot leak containers."""

        self.timeout_seconds = config["timeout_seconds"]
        self._client = None
        self._container = None
        self._owned_containers: list[Any] = []
        self._owned_networks: list[Any] = []
        self._services: list[tuple[str, Any, bool]] = []
        self._network = None
        self._topology_id = uuid4().hex
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
            topology = config.get("topology")
            service_count = 0 if topology is None else len(topology["services"])
            with docker_operation(
                "sandbox.start",
                topology_id=self._topology_id,
                attributes={"harnest.docker.service_count": service_count},
            ):
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
        """Start owned services before probing the isolated execution container."""

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
        topology = config.get("topology")
        if topology is not None:
            self._start_topology(topology)
        self._startup_phase = "execution image resolution"
        image = self._image(
            image=config["image"],
            docker_path=config["docker_path"],
            resource="execution",
        )
        check_startup()
        self._startup_phase = "container creation and start"
        options = {
            "image": image,
            "detach": True,
            "tty": False,
            "entrypoint": ["python3", "-c", "import time; time.sleep(100000000)"],
            # The primary container never runs autonomous image health checks.
            "healthcheck": {"test": ["NONE"]},
            "labels": _MANAGED_LABELS
            if topology is None
            else _resource_labels(self._topology_id, resource="sandbox"),
            "network_mode": "bridge" if config["network_enabled"] else "none",
        }
        if topology is None:
            self._container = self._client.containers.run(**options)
        else:
            # Attach atomically at create time: Docker forbids connecting a
            # container whose initial network mode is the private ``none`` mode.
            options.pop("network_mode")
            options.update(self._network_options(aliases=["sandbox"]))
            self._container = self._client.containers.create(**options)
            check_startup()
            self._container.start()
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

    def _start_topology(self, topology: dict[str, Any]) -> None:
        """Create one private bridge and start each bounded service in order."""

        self._startup_phase = "topology network creation"
        internal = topology["network"]["internal"]
        with docker_operation(
            "network.create",
            topology_id=self._topology_id,
            attributes={"harnest.docker.network.internal": internal},
        ):
            self._network = self._client.networks.create(
                f"harnest-{self._topology_id}",
                driver="bridge",
                internal=internal,
                attachable=False,
                labels=_resource_labels(
                    self._topology_id, resource="sandbox-network"
                ),
            )
        for service in topology["services"]:
            self._start_service(service)

    def _start_service(self, service: dict[str, Any]) -> None:
        """Start one service with its own limits and optional health contract."""

        name = service["name"]
        with docker_operation(
            "service.start",
            topology_id=self._topology_id,
            attributes={
                "harnest.docker.service.name": name,
                "harnest.docker.service.readiness": service["readiness"] is not None,
            },
        ):
            self._start_service_owned(service)

    def _start_service_owned(self, service: dict[str, Any]) -> None:
        """Allocate, attach, and prove readiness for one owned service."""

        name = service["name"]
        self._startup_phase = f"service {name!r} image resolution"
        image = self._image(
            image=service["image"],
            docker_path=service["docker_path"],
            resource=f"service {name!r}",
        )
        self._startup_phase = f"service {name!r} container creation and start"
        readiness = service["readiness"]
        options: dict[str, Any] = {
            "image": image,
            "detach": True,
            "tty": False,
            "healthcheck": _healthcheck(readiness),
            "labels": _resource_labels(
                self._topology_id,
                resource="sandbox-service",
                service=name,
            ),
            "_harnest_limits": service["harnest_resource_limits"],
        }
        if service["command"]:
            options["command"] = service["command"]
        if service["environment"]:
            options["environment"] = service["environment"]
        # Ports are internal service declarations. Docker bridge peers can reach
        # them directly, so passing Docker's high-level ``ports`` option here
        # would accidentally create host bindings.
        options.update(
            self._network_options(aliases=[name, *service["aliases"]])
        )
        container = self._client.containers.create(**options)
        check_startup()
        container.start()
        self._services.append((name, container, readiness is not None))
        if readiness is not None:
            self._wait_ready(name, container)

    def _network_options(self, *, aliases: list[str]) -> dict[str, Any]:
        """Build one atomic attachment to the exact extension-owned bridge."""

        endpoint = self._client.api.create_endpoint_config(aliases=aliases)
        return {
            "network": self._network.name,
            "networking_config": {self._network.name: endpoint},
        }

    def _wait_ready(self, name: str, container: Any) -> None:
        """Wait within startup authority for Docker to report service health."""

        self._startup_phase = f"service {name!r} readiness"
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            check_startup()
            if time.monotonic() >= deadline:
                raise TimeoutError("Docker service readiness deadline exceeded")
            constrain_startup_timeout(self._client, self.timeout_seconds)
            container.reload()
            state = container.attrs.get("State", {})
            status = state.get("Status")
            health = state.get("Health", {}).get("Status")
            if status != "running":
                raise DockerStartupError(self._startup_phase, "service exited")
            if health == "healthy":
                return
            if health == "unhealthy":
                raise DockerStartupError(self._startup_phase, "unhealthy service")
            time.sleep(0.05)

    def _image(
        self,
        *,
        image: str | None,
        docker_path: str | None,
        resource: str,
    ) -> str:
        """Resolve an immutable image ID and reject unbudgeted declared volumes."""

        from docker.errors import ImageNotFound

        constrain_startup_timeout(self._client, self.timeout_seconds)
        if docker_path is not None:
            self._startup_phase = f"{resource} image build"
            image, _logs = self._client.images.build(
                path=docker_path, rm=True
            )
        else:
            self._startup_phase = f"{resource} image lookup"
            try:
                image = self._client.images.get(image)
            except ImageNotFound:
                check_startup()
                constrain_startup_timeout(self._client, self.timeout_seconds)
                self._startup_phase = f"{resource} image pull"
                image = self._client.images.pull(image)
        check_startup()
        self._startup_phase = f"{resource} image policy validation"
        if image.attrs.get("Config", {}).get("Volumes"):
            raise ValueError(
                "sandbox images must not declare volumes; use budgeted /tmp scratch"
            )
        return image.id

    def execute(self, request: SandboxRequest) -> SandboxResult:
        """Execute Python with a host deadline and preserve its terminal status."""

        with docker_operation(
            "sandbox.execute",
            topology_id=self._topology_id,
            attributes={
                "harnest.docker.service_count": len(self._services),
            },
        ):
            self._check_services()
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

    def _check_services(self) -> None:
        """Fail and poison the topology if a retained service is no longer ready."""

        for name, container, requires_health in self._services:
            constrain_startup_timeout(self._client, self.timeout_seconds)
            container.reload()
            state = container.attrs.get("State", {})
            running = state.get("Status") == "running"
            healthy = state.get("Health", {}).get("Status") == "healthy"
            if not running or (requires_health and not healthy):
                self._guard_poisoned = True
                raise RuntimeError(f"Docker sandbox service {name!r} is unavailable")

    def _retain_container(self, container: Any) -> None:
        """Record an exact container handle before any later startup operation."""

        self._owned_containers.append(container)

    def _retain_network(self, network: Any) -> None:
        """Record an exact network handle before any container attachment."""

        self._owned_networks.append(network)

    def _forget_container(self, container: Any) -> None:
        """Forget one container only after removal or confirmed absence."""

        self._owned_containers = [
            owned for owned in self._owned_containers if owned is not container
        ]

    def _forget_network(self, network: Any) -> None:
        """Forget one network only after removal or confirmed absence."""

        self._owned_networks = [
            owned for owned in self._owned_networks if owned is not network
        ]

    def close(self) -> None:
        """Retain cleanup ownership until resource removal is confirmed."""

        close_guarded_executor(self)


def create_docker_executor(
    config: dict[str, Any], output_limit: int
) -> DockerExecutor:
    """Construct one Docker executor behind a focused test boundary."""

    return DockerExecutor(config, output_limit)


def _healthcheck(readiness: dict[str, Any] | None) -> dict[str, Any]:
    """Translate bounded readiness into Docker's nanosecond healthcheck fields."""

    if readiness is None:
        return {"test": ["NONE"]}
    return {
        "test": ["CMD", *readiness["command"]],
        "interval": int(readiness["interval_seconds"] * 1_000_000_000),
        "timeout": int(readiness["timeout_seconds"] * 1_000_000_000),
        "retries": readiness["retries"],
        "start_period": int(readiness["start_period_seconds"] * 1_000_000_000),
    }


def _retain_uncertain_cleanup(error: BaseException, executor: DockerExecutor) -> None:
    """Attach cleanup ownership without replacing the primary startup failure."""

    error.failed_executor = executor
    error.cleanup_unconfirmed = True
    message = str(error)
    suffix = "owned Docker resource cleanup could not be confirmed"
    if suffix not in message:
        error.args = (f"{message}; {suffix}",)


__all__ = ["DockerExecutor", "DockerStartupError", "create_docker_executor"]
