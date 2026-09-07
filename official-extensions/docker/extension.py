"""Docker-backed, framework-neutral sandbox definitions for Harnest applications."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from harnest.extensions import Extension
from harnest.sandbox import (
    Sandbox,
    SandboxBudget,
    SandboxNetworkMode,
    SandboxNetworkPolicy,
    SandboxPolicyUnsupportedError,
    SandboxProviderCapabilities,
    SandboxRequest,
    SandboxResult,
)

from .lib.backend import create_docker_backend, validate_adapter_options
from .lib.topology import DockerNetwork, DockerReadiness, DockerScope, DockerService


DOCKER_SANDBOX_CAPABILITIES = SandboxProviderCapabilities(
    network_modes=frozenset(
        {SandboxNetworkMode.NONE, SandboxNetworkMode.UNRESTRICTED}
    ),
)


class DockerSandboxProvider:
    """Bind one extension-owned Docker backend to its configured network policy."""

    sandbox_capabilities = DOCKER_SANDBOX_CAPABILITIES

    def __init__(
        self,
        backend: Any,
        network_policy: SandboxNetworkPolicy,
    ) -> None:
        """Bind one backend to the policy used when its container was configured."""

        self._backend = backend
        self._network_policy = network_policy

    def execute(self, request: SandboxRequest) -> SandboxResult:
        """Reject policy substitution before delegating generic Python execution."""

        if request.network_policy != self._network_policy:
            raise SandboxPolicyUnsupportedError(
                "Docker sandbox requests must retain the configured network policy"
            )
        return self._backend.execute(request)

    def close(self) -> None:
        """Release the extension backend when the owning sandbox runtime closes."""

        closer = getattr(self._backend, "close", None)
        if callable(closer):
            closer()


def docker_sandbox(
    *,
    image: str | None = None,
    docker_path: str | None = None,
    base_url: str | None = None,
    network_policy: SandboxNetworkPolicy | None = None,
    services: tuple[DockerService, ...] | list[DockerService] = (),
    network: DockerNetwork | None = None,
    timeout_seconds: int = 300,
    options: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    max_output_bytes: int = 1_048_576,
    scope: DockerScope = DockerScope.EXECUTION,
    budget: SandboxBudget | None = None,
    max_scopes: int = 8,
) -> Sandbox:
    """Create a lazy Docker sandbox without importing an agent framework."""

    policy = SandboxNetworkPolicy.none() if network_policy is None else network_policy
    if not isinstance(policy, SandboxNetworkPolicy):
        raise TypeError("Docker sandbox network_policy must be SandboxNetworkPolicy")
    DOCKER_SANDBOX_CAPABILITIES.require(policy)
    adapter_options = validate_adapter_options(options)

    # Constructing a backend only validates immutable settings. Docker remains
    # untouched until a native adapter executes its first request.
    configured = create_docker_backend(
        image=image,
        docker_path=docker_path,
        base_url=base_url,
        external_network=policy.mode == SandboxNetworkMode.UNRESTRICTED,
        services=services,
        network=network,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        scope=scope,
        budget=budget,
        max_scopes=max_scopes,
    )

    def build_provider() -> DockerSandboxProvider:
        """Give each native adapter independent Docker resource ownership."""

        return DockerSandboxProvider(configured.new_backend(), policy)

    return Sandbox.provider(
        build_provider,
        name="docker",
        timeout_seconds=timeout_seconds,
        metadata=metadata,
        network_policy=policy,
        adapter_options=adapter_options,
    )


class DockerExtension(Extension):
    """Expose Docker sandbox definitions without owning framework resources."""

    def sandbox(
        self,
        *,
        image: str | None = None,
        docker_path: str | None = None,
        base_url: str | None = None,
        network_policy: SandboxNetworkPolicy | None = None,
        services: tuple[DockerService, ...] | list[DockerService] = (),
        network: DockerNetwork | None = None,
        timeout_seconds: int = 300,
        options: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        max_output_bytes: int = 1_048_576,
        scope: DockerScope = DockerScope.EXECUTION,
        budget: SandboxBudget | None = None,
        max_scopes: int = 8,
    ) -> Sandbox:
        """Create a portable Docker sandbox definition from authored options."""

        return docker_sandbox(
            image=image,
            docker_path=docker_path,
            base_url=base_url,
            network_policy=network_policy,
            services=services,
            network=network,
            timeout_seconds=timeout_seconds,
            options=options,
            metadata=metadata,
            max_output_bytes=max_output_bytes,
            scope=scope,
            budget=budget,
            max_scopes=max_scopes,
        )

    def service(
        self,
        *,
        name: str,
        image: str | None = None,
        docker_path: str | None = None,
        command: tuple[str, ...] | list[str] = (),
        environment: Mapping[str, str] | None = None,
        ports: tuple[int, ...] | list[int] = (),
        aliases: tuple[str, ...] | list[str] = (),
        readiness: DockerReadiness | None = None,
        budget: SandboxBudget | None = None,
    ) -> DockerService:
        """Declare one bounded service container without contacting Docker."""

        return DockerService(
            name=name,
            image=image,
            docker_path=docker_path,
            command=command,
            environment={} if environment is None else environment,
            ports=ports,
            aliases=aliases,
            readiness=readiness,
            budget=SandboxBudget() if budget is None else budget,
        )

    def network(self, *, internal: bool = True) -> DockerNetwork:
        """Declare the extension-owned bridge that joins a sandbox topology."""

        return DockerNetwork(internal=internal)

    def readiness(
        self,
        *,
        command: tuple[str, ...] | list[str],
        interval_seconds: float = 1.0,
        timeout_seconds: float = 2.0,
        retries: int = 30,
        start_period_seconds: float = 0.0,
    ) -> DockerReadiness:
        """Declare a bounded Docker health command for one service."""

        return DockerReadiness(
            command=command,
            interval_seconds=interval_seconds,
            timeout_seconds=timeout_seconds,
            retries=retries,
            start_period_seconds=start_period_seconds,
        )


extension = DockerExtension()
docker = extension


__all__ = [
    "DOCKER_SANDBOX_CAPABILITIES",
    "DockerNetwork",
    "DockerReadiness",
    "DockerScope",
    "DockerService",
    "DockerExtension",
    "DockerSandboxProvider",
    "docker",
    "docker_sandbox",
    "extension",
]
