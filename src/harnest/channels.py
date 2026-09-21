"""Provider-neutral chat-platform channel contract and adapter registry.

A channel lets a user message a Harnest agent through a chat platform (Slack,
Teams, or a custom system) with the agent replying in the same conversation.
This module defines the transport-agnostic contract only: no platform, and no
provider such as Fused, is a dependency of anything here. Concrete platforms
are shipped as extensions that register a ChannelAdapter factory under their
own name; Harnest core never imports a specific extension.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .channel_storage import (
    ChannelEvent,
    ChannelReply,
    ChannelStore,
    ChannelStoreConflictError,
)
from .channel_store_memory import MemoryChannelStore
from .channel_worker import ChannelWorker
from .channel_http import ChannelHTTPInvoker


class ChannelError(RuntimeError):
    """Base class for channel configuration and adapter failures."""


class ChannelAdapterConflictError(ChannelError):
    """Two extensions registered a channel adapter under the same name."""


class ChannelAdapterNotFoundError(ChannelError):
    """No extension registered a channel adapter under the requested name."""


@dataclass(frozen=True, slots=True)
class ChannelBinding:
    """Local policy connecting one platform conversation scope to this agent.

    ``extension`` names the registered ChannelAdapter factory that owns the
    platform transport (for example a Fused-backed Slack extension, or a
    direct-API extension); the binding itself never depends on how that
    extension talks to the platform. Empty allow-lists mean nothing is
    authorized yet, not that everything is; an adapter must fail closed.
    """

    platform: str
    extension: str
    allowed_installations: tuple[str, ...] = ()
    allowed_conversations: tuple[str, ...] = ()
    config: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Reject a binding that cannot identify its platform or extension."""

        if not self.platform or not self.extension:
            raise ValueError("ChannelBinding requires both platform and extension")


class ChannelAdapter(ABC):
    """Platform-specific transport that normalizes events and sends replies.

    Extensions implement this contract without exposing raw provider drivers.
    Normalization is synchronous and offline so fixtures can be tested without
    live credentials; sending is the only network-owned operation.
    """

    platform: str

    async def start(self) -> None:
        """Initialize provider-owned clients before accepting invocations."""

    @abstractmethod
    def normalize_event(self, raw: Mapping[str, Any]) -> ChannelEvent:
        """Parse one provider payload into a ChannelEvent without any I/O."""

    @abstractmethod
    async def send_reply(self, reply: ChannelReply) -> Mapping[str, Any]:
        """Send content through the transport and return an opaque receipt."""

    async def close(self) -> None:
        """Release provider-owned clients after invocations have drained."""


ChannelAdapterFactory = Callable[[Mapping[str, Any]], ChannelAdapter]

_ADAPTER_FACTORIES: dict[str, ChannelAdapterFactory] = {}


def register_channel_adapter(extension: str, factory: ChannelAdapterFactory) -> None:
    """Register one packaged extension's adapter factory under its unique name."""

    if not extension:
        raise ValueError("channel adapter extension name must be non-empty")
    if extension in _ADAPTER_FACTORIES:
        raise ChannelAdapterConflictError(
            f"channel adapter extension {extension!r} is already registered"
        )
    _ADAPTER_FACTORIES[extension] = factory


def create_channel_adapter(extension: str, config: Mapping[str, Any]) -> ChannelAdapter:
    """Build the adapter a binding names, raising if its extension is absent."""

    factory = _ADAPTER_FACTORIES.get(extension)
    if factory is None:
        raise ChannelAdapterNotFoundError(
            f"no channel adapter extension registered as {extension!r}"
        )
    return factory(config)


def registered_channel_adapters() -> tuple[str, ...]:
    """List currently registered extension names for inspection and errors."""

    return tuple(sorted(_ADAPTER_FACTORIES))


def discover_channel_bindings(directory: Path) -> tuple[ChannelBinding, ...]:
    """Load every ChannelBinding exported by a project's channels/ directory.

    Mirrors the discovery convention used for tools and MCP clients: one file
    per binding, named after the platform, exporting a callable ``binding()``.
    """

    from .bundle import _load_export, _reject_extra_exports, _resource_files

    bindings = []
    for path in _resource_files(directory, kind="channel"):
        module, factory = _load_export(path, "binding")
        if not callable(factory):
            raise ChannelError(
                f"channel module {path} must export callable binding(); "
                f"got {type(factory).__name__}"
            )
        value = factory()
        if not isinstance(value, ChannelBinding):
            raise ChannelError(
                f"channel binding() factory in {path} must return ChannelBinding; "
                f"got {type(value).__name__}"
            )
        _reject_extra_exports(
            module, path, "binding", kind="channel binding",
            predicate=lambda item: isinstance(item, ChannelBinding)
            or (callable(item) and getattr(item, "__module__", None) == module.__name__),
        )
        bindings.append(value)
    return tuple(bindings)


__all__ = [
    "ChannelAdapter",
    "ChannelAdapterConflictError",
    "ChannelAdapterFactory",
    "ChannelAdapterNotFoundError",
    "ChannelBinding",
    "ChannelError",
    "ChannelEvent",
    "ChannelHTTPInvoker",
    "ChannelWorker",
    "ChannelReply",
    "ChannelStore",
    "ChannelStoreConflictError",
    "MemoryChannelStore",
    "create_channel_adapter",
    "discover_channel_bindings",
    "register_channel_adapter",
    "registered_channel_adapters",
]
