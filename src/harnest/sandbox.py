"""Portable isolated Python execution, lowered through native framework adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .sandbox_policy import (
    SandboxBudget,
    SandboxNetworkMode,
    SandboxNetworkPolicy,
    SandboxPolicyUnsupportedError,
    SandboxProviderCapabilities,
)
from .sandbox_control import SandboxCancelledError, cleanup_control, control
from .sandbox_runtime import (
    SandboxExecutionError,
    SandboxInputFilesUnsupportedError,
    validate_backend,
)
from .sandbox_types import (
    SandboxBackend, SandboxContext, SandboxFile, SandboxProvider, SandboxRequest,
    SandboxResult, SandboxStatus,
    freeze_sandbox_metadata, validate_timeout,
)


@dataclass(frozen=True, slots=True)
class Sandbox:
    """Declare one lazy sandbox usable by managed ADK and LangGraph agents.

    A sandbox is a code-execution boundary, not a policy label. The backend
    returned by ``factory`` implements ``SandboxBackend.execute`` and owns the
    actual isolation guarantees. Legacy ADK executors remain ADK-only providers.
    Compilation never connects to provider infrastructure.
    """

    factory: Callable[[], Any] = field(repr=False)
    backend: str = "custom"
    timeout_seconds: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False)
    # Keep this additive contract keyword-only so existing positional
    # construction cannot reinterpret private adapter options as network policy.
    network_policy: SandboxNetworkPolicy | None = field(default=None, kw_only=True)
    _executor_options: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Validate immutable authoring configuration without invoking a provider."""
        if not callable(self.factory):
            raise TypeError("sandbox factory must be callable")
        if not isinstance(self.backend, str) or not self.backend.strip():
            raise ValueError("sandbox backend name is required")
        object.__setattr__(self, "backend", self.backend.strip())
        validate_timeout(self.timeout_seconds)
        if self.network_policy is not None and not isinstance(
            self.network_policy, SandboxNetworkPolicy
        ):
            raise TypeError("sandbox network_policy must be SandboxNetworkPolicy or None")
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
        network_policy: SandboxNetworkPolicy | None = None,
        adapter_options: Mapping[str, Any] | None = None,
    ) -> "Sandbox":
        """Configure a portable provider; native ADK factories remain compatible.

        Provider-specific SDK settings belong in the factory. ``adapter_options``
        configures only the native framework adapter, while JSON metadata and an
        optional network policy are forwarded to each request. A policy requires
        provider capability declarations.
        """

        return cls(
            factory=factory,
            backend=name,
            timeout_seconds=timeout_seconds,
            metadata={} if metadata is None else metadata,
            network_policy=network_policy,
            _executor_options={} if adapter_options is None else adapter_options,
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
        return validate_backend(self.factory(), network_policy=self.network_policy)


__all__ = [
    "control", "cleanup_control", "SandboxCancelledError",
    "Sandbox", "SandboxBudget", "SandboxBackend", "SandboxContext", "SandboxExecutionError",
    "SandboxFile", "SandboxInputFilesUnsupportedError", "SandboxNetworkMode",
    "SandboxNetworkPolicy", "SandboxPolicyUnsupportedError", "SandboxProvider",
    "SandboxProviderCapabilities", "SandboxRequest", "SandboxResult", "SandboxStatus",
]
