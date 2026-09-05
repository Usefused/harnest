"""Typed authoring surface for the official Docker Harnest Extension."""

from collections.abc import Mapping
from typing import Any

from harnest.extensions import Extension
from harnest.sandbox import (
    Sandbox,
    SandboxBudget,
    SandboxNetworkPolicy,
    SandboxProviderCapabilities,
    SandboxRequest,
    SandboxResult,
)

DOCKER_SANDBOX_CAPABILITIES: SandboxProviderCapabilities

class DockerSandboxProvider:
    """Execute sandbox requests through an extension-owned Docker backend."""

    sandbox_capabilities: SandboxProviderCapabilities

    def __init__(
        self, backend: Any, network_policy: SandboxNetworkPolicy
    ) -> None: ...

    def execute(self, request: SandboxRequest) -> SandboxResult:
        """Execute one request after validating its configured network policy."""

        ...

    def close(self) -> None:
        """Release the extension-owned Docker backend."""

        ...

def docker_sandbox(
    *,
    image: str | None = ...,
    docker_path: str | None = ...,
    base_url: str | None = ...,
    network_policy: SandboxNetworkPolicy | None = ...,
    timeout_seconds: int = ...,
    options: Mapping[str, Any] | None = ...,
    metadata: Mapping[str, Any] | None = ...,
    max_output_bytes: int = ...,
    scope: str = ...,
    budget: SandboxBudget | None = ...,
    max_scopes: int = ...,
) -> Sandbox:
    """Create a lazy, framework-neutral Docker sandbox definition."""

    ...

class DockerExtension(Extension):
    """Expose Docker sandbox definitions to an authored Harnest application."""

    def sandbox(
        self,
        *,
        image: str | None = ...,
        docker_path: str | None = ...,
        base_url: str | None = ...,
        network_policy: SandboxNetworkPolicy | None = ...,
        timeout_seconds: int = ...,
        options: Mapping[str, Any] | None = ...,
        metadata: Mapping[str, Any] | None = ...,
        max_output_bytes: int = ...,
        scope: str = ...,
        budget: SandboxBudget | None = ...,
        max_scopes: int = ...,
    ) -> Sandbox:
        """Create a Docker sandbox with editor-visible configuration options."""

        ...

extension: DockerExtension
docker: DockerExtension
