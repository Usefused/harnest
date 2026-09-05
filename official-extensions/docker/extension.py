"""Docker-backed, framework-neutral sandbox definitions for Harnest applications."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from harnest.extensions import Extension
from harnest.sandbox import (
    Sandbox,
    SandboxBackend,
    SandboxBudget,
    SandboxNetworkMode,
    SandboxNetworkPolicy,
    SandboxPolicyUnsupportedError,
    SandboxProviderCapabilities,
    SandboxRequest,
    SandboxResult,
)


DOCKER_SANDBOX_CAPABILITIES = SandboxProviderCapabilities(
    network_modes=frozenset(
        {SandboxNetworkMode.NONE, SandboxNetworkMode.UNRESTRICTED}
    ),
)


class DockerSandboxProvider:
    """Adapt Harnest's compatible Docker backend to the portable provider contract."""

    sandbox_capabilities = DOCKER_SANDBOX_CAPABILITIES

    def __init__(
        self,
        backend: SandboxBackend,
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
        """Release the compatibility backend when the owning sandbox runtime closes."""

        closer = getattr(self._backend, "close", None)
        if callable(closer):
            closer()


def docker_sandbox(
    *,
    image: str | None = None,
    docker_path: str | None = None,
    base_url: str | None = None,
    network_policy: SandboxNetworkPolicy | None = None,
    timeout_seconds: int = 300,
    options: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    max_output_bytes: int = 1_048_576,
    scope: str = "execution",
    budget: SandboxBudget | None = None,
    max_scopes: int = 8,
) -> Sandbox:
    """Create a Docker sandbox without importing ADK, LangGraph, or browser code.

    This first extension phase delegates execution to ``Sandbox.container`` so
    existing identity, budget, cleanup, deadline, and result behavior remains
    compatible. The extension boundary can later own the Docker implementation
    without changing consuming ``sandbox/*.py`` modules.
    """

    policy = SandboxNetworkPolicy.none() if network_policy is None else network_policy
    if not isinstance(policy, SandboxNetworkPolicy):
        raise TypeError("Docker sandbox network_policy must be SandboxNetworkPolicy")
    DOCKER_SANDBOX_CAPABILITIES.require(policy)
    container = Sandbox.container(
        image=image,
        docker_path=docker_path,
        base_url=base_url,
        network=policy.mode == SandboxNetworkMode.UNRESTRICTED,
        timeout_seconds=timeout_seconds,
        options=options,
        metadata=metadata,
        max_output_bytes=max_output_bytes,
        scope=scope,
        budget=budget,
        max_scopes=max_scopes,
    )

    def build_provider() -> DockerSandboxProvider:
        """Give each native adapter an independently owned Docker backend."""

        return DockerSandboxProvider(container.factory(), policy)

    # Replacing the compatibility definition retains ADK parsing and retry
    # options that belong to its native adapter, without making those details
    # part of the extension's public provider contract.
    return replace(
        container,
        factory=build_provider,
        backend="docker",
        network_policy=policy,
    )


class DockerExtension(Extension):
    """Expose Docker sandbox definitions without owning framework resources."""

    def sandbox(self, **options: Any) -> Sandbox:
        """Create a portable Docker sandbox definition from authored options."""

        return docker_sandbox(**options)


extension = DockerExtension()
docker = extension


__all__ = [
    "DOCKER_SANDBOX_CAPABILITIES",
    "DockerExtension",
    "DockerSandboxProvider",
    "docker",
    "docker_sandbox",
    "extension",
]
