"""Invocation-scoped access to explicitly exported Harnest resources."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import re
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping


_CONTEXT_ATTRIBUTE = "__harnest_context_registration__"
_CONTEXT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ContextUnavailableError(RuntimeError):
    """Raised when agent context is accessed outside a Harnest invocation."""


class ContextResourceError(LookupError):
    """Raised when an invocation does not contain a requested resource."""


@dataclass(frozen=True, slots=True)
class ContextRegistration:
    """Metadata declaring the name and order of one context provider."""

    name: str
    order: int = 0

    def __post_init__(self) -> None:
        _validate_name(self.name)


@dataclass(frozen=True, slots=True)
class ContextValue:
    """One application-owned value exported into every invocation context."""

    name: str
    value: Any = field(repr=False)
    identity: str

    def __post_init__(self) -> None:
        _validate_name(self.name)
        if not isinstance(self.identity, str) or not self.identity.strip():
            raise ValueError("context value identity must be a non-empty string")


@dataclass(slots=True)
class _ContextLifetime:
    """Share revocation state with child tasks that copied the context variable."""

    active: bool = True


@dataclass(frozen=True, slots=True)
class AgentContext:
    """Stable invocation identity and its explicitly exported resources."""

    framework: str
    agent_name: str
    invocation_id: str
    user_id: str
    session_id: str
    metadata: Mapping[str, Any]
    parent_agent_name: str | None
    depth: int
    _resources: Mapping[str, Any] = field(repr=False)
    _asset_stores: Mapping[str, Any] = field(repr=False)
    _custom_stores: Mapping[str, Any] = field(repr=False)
    _plugin_bindings: Mapping[str, Any] = field(repr=False)
    _lifetime: _ContextLifetime = field(repr=False, compare=False)

    def resource(self, name: str, expected_type: type[Any] | None = None) -> Any:
        """Return one named capability without exposing the whole registry."""

        self._require_active()
        _validate_name(name)
        if name not in self._resources:
            raise ContextResourceError(
                f"context resource {name!r} is not available in this invocation"
            )
        value = self._resources[name]
        if expected_type is not None and not isinstance(value, expected_type):
            raise ContextResourceError(
                f"context resource {name!r} must be {expected_type.__name__}; "
                f"got {type(value).__name__}"
            )
        return value

    @property
    def is_root(self) -> bool:
        """Report whether this scope represents the compiled root agent."""

        return self.depth == 0

    def _require_active(self) -> None:
        if not self._lifetime.active:
            raise ContextUnavailableError(
                "Harnest context is available only during a managed invocation"
            )


_ACTIVE_CONTEXT: ContextVar[AgentContext | None] = ContextVar(
    "harnest_agent_context", default=None
)


class _ContextAccess:
    """Decorate providers and access the context active in the current task."""

    def __call__(
        self, name: str, *, order: int = 0
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        _validate_name(name)
        if not isinstance(order, int) or isinstance(order, bool):
            raise TypeError("context provider order must be an integer")

        def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
            if not callable(function):
                raise TypeError("@context can only decorate callables")
            if getattr(function, "__harnest_tool__", False):
                raise TypeError("tools cannot also be context providers")
            if hasattr(function, _CONTEXT_ATTRIBUTE):
                raise TypeError("a function may provide only one context resource")
            setattr(function, _CONTEXT_ATTRIBUTE, ContextRegistration(name, order))
            return function

        return decorate

    def current(self) -> AgentContext:
        """Return the active invocation or fail outside managed execution."""

        active = _ACTIVE_CONTEXT.get()
        if active is None:
            raise ContextUnavailableError(
                "Harnest context is available only during a managed invocation"
            )
        active._require_active()
        return active

    def resource(self, name: str, expected_type: type[Any] | None = None) -> Any:
        """Resolve a resource explicitly published by a context provider."""

        return self.current().resource(name, expected_type)

    @property
    def credentials(self) -> Any:
        """Return the private credential resolver for the active invocation."""

        # Import lazily because credential resolution itself depends on this
        # context facade. The resolver is a capability, not stored context data.
        self.current()
        from .credentials import credentials

        return credentials

    @property
    def assets(self) -> Any:
        """Return storage access scoped to the active user and session."""

        active = self.current()
        from .assets import AssetScope
        from .context_assets import ScopedAssets

        return ScopedAssets(
            AssetScope(user_id=active.user_id, session_id=active.session_id),
            active._asset_stores,
        )

    @property
    def session(self) -> Any:
        """Return application data for the current framework-owned session."""

        self.current()
        from .context_session import session

        return session

    @property
    def storage(self) -> Any:
        """Return only explicitly named custom storage capabilities."""

        self.current()
        from .context_storage import storage

        return storage

    @property
    def mcp(self) -> Any:
        """Return governed MCP access when the runtime installed a dispatcher."""

        self.current()
        from .mcp_context import mcp

        return mcp

    @property
    def plugins(self) -> Any:
        """Return non-enumerable same-process plugin capabilities."""

        self.current()
        from .plugin_runtime_context import plugins

        return plugins

    @property
    def framework(self) -> str:
        return self.current().framework

    @property
    def agent_name(self) -> str:
        return self.current().agent_name

    @property
    def invocation_id(self) -> str:
        return self.current().invocation_id

    @property
    def user_id(self) -> str:
        return self.current().user_id

    @property
    def session_id(self) -> str:
        return self.current().session_id

    @property
    def metadata(self) -> Mapping[str, Any]:
        return self.current().metadata

    @property
    def parent_agent_name(self) -> str | None:
        return self.current().parent_agent_name

    @property
    def depth(self) -> int:
        return self.current().depth

    @property
    def is_root(self) -> bool:
        return self.current().is_root


def registration_for(function: Any) -> ContextRegistration | None:
    """Return context metadata without treating imported aliases as providers."""

    value = getattr(function, _CONTEXT_ATTRIBUTE, None)
    return value if isinstance(value, ContextRegistration) else None


def create_agent_context(
    *,
    framework: str,
    agent_name: str,
    invocation_id: str,
    user_id: str,
    session_id: str,
    metadata: Mapping[str, Any],
    resources: Mapping[str, Any],
    asset_stores: Mapping[str, Any] | None = None,
    custom_stores: Mapping[str, Any] | None = None,
    plugin_bindings: Mapping[str, Any] | None = None,
) -> AgentContext:
    """Create a context with a private mutable registry for provider binding."""

    for name in resources:
        _validate_name(name)
    registry = dict(resources)
    from .plugin_runtime_context import validate_plugin_bindings

    plugins = validate_plugin_bindings(
        {} if plugin_bindings is None else plugin_bindings
    )
    return AgentContext(
        framework=framework,
        agent_name=agent_name,
        invocation_id=invocation_id,
        user_id=user_id,
        session_id=session_id,
        metadata=MappingProxyType(dict(metadata)),
        parent_agent_name=None,
        depth=0,
        _resources=MappingProxyType(registry),
        _asset_stores=MappingProxyType(dict(asset_stores or {})),
        _custom_stores=MappingProxyType(dict(custom_stores or {})),
        _plugin_bindings=plugins,
        _lifetime=_ContextLifetime(),
    )


def derive_agent_context(active: AgentContext, *, agent_name: str) -> AgentContext:
    """Create a child view that shares only its parent's revocable capabilities."""

    active._require_active()
    if not isinstance(agent_name, str) or not agent_name.strip():
        raise ValueError("derived agent_name must be a non-empty string")
    return AgentContext(
        framework=active.framework,
        agent_name=agent_name,
        invocation_id=active.invocation_id,
        user_id=active.user_id,
        session_id=active.session_id,
        metadata=active.metadata,
        parent_agent_name=active.agent_name,
        depth=active.depth + 1,
        _resources=active._resources,
        _asset_stores=active._asset_stores,
        _custom_stores=active._custom_stores,
        _plugin_bindings=active._plugin_bindings,
        _lifetime=active._lifetime,
    )


@contextmanager
def activate_agent_scope(agent_name: str | None) -> Iterator[AgentContext | None]:
    """Narrow managed identity to the framework component currently executing."""

    active = _ACTIVE_CONTEXT.get()
    if active is None or agent_name is None or active.agent_name == agent_name:
        yield active
        return
    derived = derive_agent_context(active, agent_name=agent_name)
    token = _ACTIVE_CONTEXT.set(derived)
    try:
        yield derived
    finally:
        # Derived contexts share the root lifetime; only the invocation owner
        # may revoke that authority after all nested callbacks have completed.
        _ACTIVE_CONTEXT.reset(token)


def bind_resource(active: AgentContext, name: str, value: Any) -> None:
    """Extend only the private registry created for one active invocation."""

    active._require_active()
    _validate_name(name)
    resources = active._resources
    if not isinstance(resources, MappingProxyType):  # pragma: no cover - constructor owns it
        raise TypeError("agent context resource registry is invalid")
    registry = resources.copy()
    if name in registry:
        raise ValueError(f"context resource {name!r} is already bound")
    registry[name] = value
    object.__setattr__(active, "_resources", MappingProxyType(registry))


def revoke_context(active: AgentContext) -> None:
    """Invalidate copied task contexts when their owning invocation finishes."""

    active._lifetime.active = False
    from .plugin_runtime_context import revoke_plugin_bindings

    revoke_plugin_bindings(active._plugin_bindings)


@contextmanager
def activate_context(active: AgentContext) -> Iterator[None]:
    """Bind one invocation to the current async task and restore nesting safely."""

    active._require_active()
    token = _ACTIVE_CONTEXT.set(active)
    try:
        from .plugin_runtime_context import activate_plugin_bindings

        with activate_plugin_bindings(active._plugin_bindings):
            yield
    finally:
        _ACTIVE_CONTEXT.reset(token)


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not _CONTEXT_NAME.fullmatch(name):
        raise ValueError(
            "context resource name must be a valid public Python identifier"
        )


context = _ContextAccess()


__all__ = [
    "AgentContext",
    "ContextRegistration",
    "ContextResourceError",
    "ContextUnavailableError",
    "activate_agent_scope",
    "context",
    "derive_agent_context",
]
