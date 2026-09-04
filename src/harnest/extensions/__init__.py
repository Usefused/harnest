"""Public APIs for reusable Harnest Extensions, separate from Agent Plugins.

The legacy runtime-plugin types are aliases to the same ownership machinery;
mixing migrated and legacy packages must not create a second resource lifetime.
"""

from harnest.plugins import (
    Plugin as Extension,
    PluginContext as ExtensionContext,
    PluginContextUnavailableError as ExtensionContextUnavailableError,
    PluginImportError as ExtensionImportError,
    PluginNamespaceError as ExtensionNamespaceError,
    PluginStartContext as ExtensionStartContext,
    plugin_mutation as _plugin_mutation,
)
from contextlib import asynccontextmanager
from typing import AsyncIterator, Literal


@asynccontextmanager
async def extension_mutation(
    extension_name: str, operation: str, *, trigger: Literal["agent", "user"]
) -> AsyncIterator[None]:
    """Keep canonical calls on the existing privacy-safe durable audit boundary."""

    async with _plugin_mutation(extension_name, operation, trigger=trigger):
        yield

__all__ = [
    "Extension", "ExtensionContext", "ExtensionContextUnavailableError",
    "ExtensionImportError", "ExtensionNamespaceError", "ExtensionStartContext",
    "extension_mutation",
]
