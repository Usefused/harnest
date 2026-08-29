"""Explicit construction order for framework-neutral runtime capabilities."""

from __future__ import annotations

from typing import Any, Sequence

from .application import RuntimeCapabilities
from .runtime_contract import RuntimeDriver
from .runtime_extensions import ExtensionRuntimeDriver
from .runtime_session import StorageRuntimeDriver


def build_runtime_pipeline(
    driver: RuntimeDriver,
    capabilities: RuntimeCapabilities,
    extensions: Sequence[Any],
    *,
    manage_credential_provider: bool = True,
) -> RuntimeDriver:
    """Wrap one backend in the single supported capability ownership order."""

    if not isinstance(capabilities, RuntimeCapabilities):
        raise TypeError("capabilities must be RuntimeCapabilities")
    # Storage is closest to the backend so lifecycle hooks observe durable
    # session state, while the extension wrapper owns application resources
    # and credentials around the complete storage-backed operation.
    current: RuntimeDriver = _with_storage(driver, capabilities)
    if not (
        extensions
        or capabilities.context_values
        or capabilities.credential_provider is not None
        or capabilities.asset_stores
    ):
        return current
    return ExtensionRuntimeDriver(
        current,
        extensions,
        context_values=capabilities.context_values,
        asset_stores=capabilities.asset_stores,
        credential_provider=capabilities.credential_provider,
        manage_credential_provider=manage_credential_provider,
    )


def _with_storage(
    driver: RuntimeDriver, capabilities: RuntimeCapabilities
) -> RuntimeDriver:
    """Install the storage wrapper only when the application owns storage."""

    if not (
        capabilities.session_store is not None
        or capabilities.checkpointer is not None
        or capabilities.asset_store is not None
        or capabilities.asset_stores
    ):
        return driver
    return StorageRuntimeDriver(
        driver,
        capabilities.session_store,
        capabilities.checkpointer,
        capabilities.asset_store,
        *capabilities.asset_stores.values(),
    )


__all__ = ["build_runtime_pipeline"]
