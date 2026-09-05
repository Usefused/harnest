"""Lazy sandbox ownership and privacy-safe execution accounting."""

from __future__ import annotations

import threading
import asyncio
import inspect
from typing import Any, Callable

from .logging import get_logger
from .sandbox_control import SandboxCancelledError, execution_control
from .sandbox_policy import (
    SandboxNetworkPolicy,
    SandboxPolicyUnsupportedError,
    SandboxProviderCapabilities,
)
from .sandbox_types import SandboxBackend, SandboxStatus


_AUDIT = get_logger("harnest.audit")


class SandboxExecutionError(RuntimeError):
    """A provider failed without exposing its private exception payload."""

    def __init__(self, message: str, *, status: SandboxStatus = SandboxStatus.FAILED) -> None:
        """Expose a bounded machine-readable outcome alongside a sanitized message."""
        super().__init__(message)
        self.status = status


class SandboxProviderContractError(TypeError):
    """An adapter cannot use the configured provider or its result type."""


class SandboxInputFilesUnsupportedError(ValueError):
    """A provider explicitly cannot materialize the requested input files."""


class SandboxRuntime:
    """Create one provider per native adapter, only on its first execution."""

    def __init__(self, definition: Any, framework: str) -> None:
        """Keep provider construction isolated from compiler and model setup."""
        self.definition = definition
        self.framework = framework
        self._backend: Any = None
        self._lock = threading.Lock()
        self._closed = False
        self._close_lock = asyncio.Lock()
        self._condition = threading.Condition(self._lock)
        self._active_operations = 0

    def _get_backend(self) -> Any:
        """Serialize first construction without serializing provider execution."""
        with execution_control(self.definition.timeout_seconds) as control, control.acquire(self._lock):
            if self._closed:
                raise RuntimeError("sandbox runtime is closed")
            if self._backend is None:
                self._backend = self.definition.build()
            return self._backend

    def _begin_close(self) -> Any:
        """Revoke future admission without starting a previously unused provider."""
        with self._condition:
            self._closed = True
            while self._active_operations:
                self._condition.wait()
            return self._backend

    async def aclose(self) -> None:
        """Close once after successful cleanup; retain failed cleanup for retry."""
        async with self._close_lock:
            backend = await asyncio.to_thread(self._begin_close)
            closer = getattr(backend, "close", None) or getattr(backend, "aclose", None)
            if callable(closer):
                result = await asyncio.to_thread(closer)
                if inspect.isawaitable(result):
                    await result
            self._backend = None

    def _run_operation(self, backend: Any, operation: Callable[[Any], Any]) -> Any:
        """Drain admitted work before cleanup, rejecting new work after shutdown."""
        with self._condition:
            if self._closed:
                raise RuntimeError("sandbox runtime is closed")
            self._active_operations += 1
        try:
            return operation(backend)
        finally:
            with self._condition:
                self._active_operations -= 1
                self._condition.notify_all()

    def run(self, operation: Callable[[Any], Any]) -> Any:
        """Emit one correlated outcome without logging code, files, or results."""
        try:
            with execution_control(self.definition.timeout_seconds) as control:
                backend = self._get_backend()
                control.check()
                result = self._run_operation(backend, operation)
        except SandboxCancelledError:
            self._audit("failure")
            raise
        except SandboxProviderContractError:
            self._audit("failure")
            raise SandboxExecutionError(
                "portable sandbox providers must implement SandboxBackend.execute "
                "and return SandboxResult; named sandbox capabilities do not "
                "accept native ADK executors"
            ) from None
        except SandboxInputFilesUnsupportedError:
            self._audit("failure")
            raise SandboxExecutionError(
                "this sandbox provider does not support input_files; omit input_files "
                "or assign a provider that implements file transfer"
            ) from None
        except Exception as error:
            self._audit("failure")
            # SDK exceptions can embed credentials, code, and complete output.
            # Explicit build() remains available for trusted configuration checks.
            raise SandboxExecutionError(
                f"sandbox execution failed ({type(error).__name__}); "
                "check provider configuration and its execution contract",
                status=SandboxStatus.TIMED_OUT if isinstance(error, TimeoutError) else SandboxStatus.FAILED,
            ) from None
        self._audit("success" if getattr(result, "status", SandboxStatus.SUCCEEDED) == SandboxStatus.SUCCEEDED else "failure")
        return result

    def _audit(self, outcome: str) -> None:
        """Use only fixed low-cardinality labels at the OTEL audit boundary."""
        _AUDIT.info(
            "sandbox.execute",
            operation="sandbox.execute",
            trigger="agent",
            outcome=outcome,
            framework=self.framework,
            # Provider names can be application-controlled and high-cardinality.
            backend="provider",
        )


def validate_backend(
    value: Any, *, network_policy: SandboxNetworkPolicy | None = None,
) -> Any:
    """Accept supported executors and validate explicit policy before execution."""
    if isinstance(value, SandboxBackend) and callable(value.execute):
        _validate_network_capabilities(value, network_policy)
        return value
    # Portable providers do not require importing either framework. Native ADK
    # compatibility is checked only when the portable protocol is absent.
    try:
        from google.adk.code_executors import BaseCodeExecutor
    except ImportError:
        BaseCodeExecutor = ()
    if isinstance(value, BaseCodeExecutor):
        if network_policy is not None:
            raise SandboxPolicyUnsupportedError(
                "native ADK sandbox providers cannot declare Harnest network-policy capabilities"
            )
        return value
    raise TypeError("sandbox provider must return SandboxBackend or ADK BaseCodeExecutor")


def _validate_network_capabilities(
    backend: Any, policy: SandboxNetworkPolicy | None,
) -> None:
    """Require typed provider guarantees whenever Harnest owns network policy."""
    if policy is None:
        return
    capabilities = getattr(backend, "sandbox_capabilities", None)
    if not isinstance(capabilities, SandboxProviderCapabilities):
        raise SandboxPolicyUnsupportedError(
            "sandbox providers with network_policy must declare "
            "SandboxProviderCapabilities as sandbox_capabilities"
        )
    capabilities.require(policy)
