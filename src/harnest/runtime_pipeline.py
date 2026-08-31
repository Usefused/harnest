"""Explicit construction order for framework-neutral runtime capabilities."""

from __future__ import annotations

from typing import Any, Sequence

from .application import RuntimeCapabilities
from .runtime_contract import RuntimeDriver
from .runtime_extensions import ExtensionRuntimeDriver
from .plugin_runtime_driver import PluginRuntimeDriver
from .plugin_runtime_manager import PluginRuntimeManager
from .runtime_session import StorageRuntimeDriver
from .session import SessionStore


def build_runtime_pipeline(
    driver: RuntimeDriver,
    capabilities: RuntimeCapabilities,
    extensions: Sequence[Any],
    *,
    manage_credential_provider: bool = True,
    plugin_manager: PluginRuntimeManager | None = None,
) -> RuntimeDriver:
    """Wrap one backend in the single supported capability ownership order."""

    if not isinstance(capabilities, RuntimeCapabilities):
        raise TypeError("capabilities must be RuntimeCapabilities")
    session_store = _session_context_store(driver, capabilities)
    if plugin_manager is not None and not isinstance(
        plugin_manager, PluginRuntimeManager
    ):
        raise TypeError("plugin_manager must be PluginRuntimeManager")
    # Every Harnest invocation owns identity and revocation context, even when
    # an agent declares no optional capabilities. Making this wrapper
    # conditional caused plain managed agents and MCP-only agents to observe
    # different context behavior based on unrelated configuration.
    current: RuntimeDriver = ExtensionRuntimeDriver(
        driver,
        extensions,
        context_values=capabilities.context_values,
        asset_stores=capabilities.asset_stores,
        custom_stores=capabilities.custom_stores,
        skill_registry=capabilities.skill_registry,
        session_store=session_store,
        credential_provider=capabilities.credential_provider,
        manage_credential_provider=manage_credential_provider,
        plugin_bindings=(
            None
            if plugin_manager is None
            else plugin_manager.invocation_bindings
        ),
    )
    if plugin_manager is not None:
        current = PluginRuntimeDriver(current, plugin_manager)
    # Storage is outermost so plugin start contexts see live custom stores and
    # shutdown cannot close those stores before plugins release their handles.
    return _with_storage(current, capabilities)


async def start_runtime_pipeline(driver: RuntimeDriver) -> None:
    """Eagerly start the explicit final wrapper when a host owns a lifespan."""

    starter = getattr(driver, "start", None)
    if callable(starter):
        await starter()


def _with_storage(
    driver: RuntimeDriver, capabilities: RuntimeCapabilities
) -> RuntimeDriver:
    """Install the storage wrapper only when the application owns storage."""

    if not (
        capabilities.session_store is not None
        or capabilities.checkpointer is not None
        or capabilities.asset_store is not None
        or capabilities.asset_stores
        or capabilities.custom_stores
    ):
        return driver
    return StorageRuntimeDriver(
        driver, storage_registry=capabilities.storage_registry
    )


def _session_context_store(
    driver: RuntimeDriver, capabilities: RuntimeCapabilities
) -> SessionStore | None:
    """Resolve only portable storage that can provide an invocation lease."""

    configured = capabilities.session_store
    if isinstance(configured, SessionStore):
        return configured
    # Host-injected LangGraph storage belongs to the backend, so the explicit
    # property is the only supported way for outer lifecycle hooks to reuse it.
    candidate = getattr(driver, "session_context_store", None)
    return candidate if isinstance(candidate, SessionStore) else None


__all__ = ["build_runtime_pipeline", "start_runtime_pipeline"]
