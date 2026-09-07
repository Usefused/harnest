"""Typed authoring surface for the official Docker Harnest Extension."""

from collections.abc import Mapping
from enum import Enum
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

class DockerNetwork:
    """Configure one extension-owned bridge shared only by this sandbox."""

    internal: bool
    def __init__(self, *, internal: bool = ...) -> None: ...

class DockerScope(str, Enum):
    """Choose how long one owned Docker topology may be retained."""

    EXECUTION: DockerScope
    INVOCATION: DockerScope
    SESSION: DockerScope

class DockerReadiness:
    """Wait for a declared Docker health command before sandbox execution."""

    command: tuple[str, ...]
    interval_seconds: float
    timeout_seconds: float
    retries: int
    start_period_seconds: float
    def __init__(
        self,
        *,
        command: tuple[str, ...] | list[str],
        interval_seconds: float = ...,
        timeout_seconds: float = ...,
        retries: int = ...,
        start_period_seconds: float = ...,
    ) -> None: ...

class DockerService:
    """Declare one bounded service container on the private sandbox network."""

    name: str
    image: str | None
    docker_path: str | None
    command: tuple[str, ...]
    environment: Mapping[str, str]
    ports: tuple[int, ...]
    aliases: tuple[str, ...]
    readiness: DockerReadiness | None
    budget: SandboxBudget
    def __init__(
        self,
        *,
        name: str,
        image: str | None = ...,
        docker_path: str | None = ...,
        command: tuple[str, ...] | list[str] = ...,
        environment: Mapping[str, str] = ...,
        ports: tuple[int, ...] | list[int] = ...,
        aliases: tuple[str, ...] | list[str] = ...,
        readiness: DockerReadiness | None = ...,
        budget: SandboxBudget = ...,
    ) -> None: ...

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
    services: tuple[DockerService, ...] | list[DockerService] = ...,
    network: DockerNetwork | None = ...,
    timeout_seconds: int = ...,
    options: Mapping[str, Any] | None = ...,
    metadata: Mapping[str, Any] | None = ...,
    max_output_bytes: int = ...,
    scope: DockerScope = ...,
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
        services: tuple[DockerService, ...] | list[DockerService] = ...,
        network: DockerNetwork | None = ...,
        timeout_seconds: int = ...,
        options: Mapping[str, Any] | None = ...,
        metadata: Mapping[str, Any] | None = ...,
        max_output_bytes: int = ...,
        scope: DockerScope = ...,
        budget: SandboxBudget | None = ...,
        max_scopes: int = ...,
    ) -> Sandbox:
        """Create a Docker sandbox with editor-visible configuration options."""

        ...

    def service(
        self,
        *,
        name: str,
        image: str | None = ...,
        docker_path: str | None = ...,
        command: tuple[str, ...] | list[str] = ...,
        environment: Mapping[str, str] | None = ...,
        ports: tuple[int, ...] | list[int] = ...,
        aliases: tuple[str, ...] | list[str] = ...,
        readiness: DockerReadiness | None = ...,
        budget: SandboxBudget | None = ...,
    ) -> DockerService:
        """Declare one bounded service container without contacting Docker."""

        ...

    def network(self, *, internal: bool = ...) -> DockerNetwork:
        """Declare the extension-owned bridge that joins a sandbox topology."""

        ...

    def readiness(
        self,
        *,
        command: tuple[str, ...] | list[str],
        interval_seconds: float = ...,
        timeout_seconds: float = ...,
        retries: int = ...,
        start_period_seconds: float = ...,
    ) -> DockerReadiness:
        """Declare a bounded Docker health command for one service."""

        ...

extension: DockerExtension
docker: DockerExtension
