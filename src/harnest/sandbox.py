"""Portable isolated Python execution, lowered through native framework adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .sandbox_policy import SandboxBudget
from .sandbox_runtime import SandboxExecutionError, validate_backend
from .sandbox_types import (
    SandboxBackend, SandboxContext, SandboxFile, SandboxRequest, SandboxResult, SandboxStatus,
    freeze_sandbox_metadata, validate_timeout,
)


@dataclass(frozen=True, slots=True)
class Sandbox:
    """Declare one lazy sandbox usable by managed ADK and LangGraph agents.

    A sandbox is a code-execution boundary, not a policy label. The backend
    returned by ``factory`` implements ``SandboxBackend.execute`` and owns the
    actual isolation guarantees. Legacy ADK executors remain ADK-only providers.
    Compilation never connects to Docker or a remote sandbox service.
    """

    factory: Callable[[], Any] = field(repr=False)
    backend: str = "custom"
    timeout_seconds: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False)
    _executor_options: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Validate immutable authoring configuration without invoking a provider."""
        if not callable(self.factory):
            raise TypeError("sandbox factory must be callable")
        if not isinstance(self.backend, str) or not self.backend.strip():
            raise ValueError("sandbox backend name is required")
        object.__setattr__(self, "backend", self.backend.strip())
        validate_timeout(self.timeout_seconds)
        object.__setattr__(self, "metadata", freeze_sandbox_metadata(self.metadata))
        object.__setattr__(self, "_executor_options", freeze_sandbox_metadata(self._executor_options))

    @classmethod
    def provider(
        cls,
        factory: Callable[[], Any],
        *,
        name: str = "custom",
        timeout_seconds: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "Sandbox":
        """Configure a portable provider; native ADK factories remain compatible.

        Provider-specific SDK settings belong in the factory. JSON metadata is
        forwarded unchanged to each request, never exposed as model arguments.
        """

        return cls(
            factory=factory,
            backend=name,
            timeout_seconds=timeout_seconds,
            metadata={} if metadata is None else metadata,
        )

    @classmethod
    def container(
        cls,
        *,
        image: str | None = None,
        docker_path: str | None = None,
        base_url: str | None = None,
        network: bool = False,
        timeout_seconds: int = 300,
        options: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        max_output_bytes: int = 1_048_576,
        scope: str = "execution",
        budget: SandboxBudget | None = None,
        max_scopes: int = 8,
    ) -> "Sandbox":
        """Run with explicit filesystem scope and Docker-enforced resource budgets.

        Each execution gets a fresh container by default. Invocation/session
        scopes reuse containers only within the same authenticated identity tuple;
        least-recently-used containers are removed at max_scopes. All scopes
        stop background processes and clear scratch files after each call.
        Docker execution is shared and does not import either framework.
        """

        from .sandbox_container import create_container_backend

        # Pure configuration is validated now; the backend starts Docker only
        # inside execute(), so compile/test inspection remains infrastructure-free.
        provider = create_container_backend(
            image=image, docker_path=docker_path, base_url=base_url,
            network=network, timeout_seconds=timeout_seconds, options=options,
            max_output_bytes=max_output_bytes, scope=scope, budget=budget,
            max_scopes=max_scopes,
        )

        def build_container() -> Any:
            """Give each native adapter its own lazily initialized provider."""
            return provider.new_backend()

        return cls(
            factory=build_container,
            backend="container",
            timeout_seconds=timeout_seconds,
            metadata={} if metadata is None else metadata,
            _executor_options={} if options is None else options,
        )

    def to_adk_executor(self) -> Any:
        """Use ADK's code_executor interface and native code/result event loop."""
        from .sandbox_adapters import adk_executor

        return adk_executor(self)

    def to_langchain_tool(self) -> Any:
        """Use LangGraph's native tool loop with a lazy, typed execution tool."""
        from .sandbox_adapters import langchain_tool

        return langchain_tool(self)

    def build(self) -> Any:
        """Construct and validate a provider only when explicitly requested."""
        return validate_backend(self.factory())


__all__ = [
    "Sandbox", "SandboxBudget", "SandboxBackend", "SandboxContext", "SandboxExecutionError",
    "SandboxFile", "SandboxRequest", "SandboxResult", "SandboxStatus",
]
