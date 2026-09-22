"""Invocation-scoped access to explicitly exported Harnest resources."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import re
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping

from .decision_output import DecisionOutput

if TYPE_CHECKING:
    from .context_memory import MemoryContext
    from .decision_runtime import DecisionContext


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
    _memory_store: Any = field(repr=False)
    _memory_application_id: str = field(repr=False)
    _skill_registry: Any = field(repr=False)
    _sandbox_registry: Any = field(repr=False)
    _skill_pins: dict[tuple[str, str, str], str] = field(repr=False)
    _extension_bindings: Mapping[str, Any] = field(repr=False)
    _lifetime: _ContextLifetime = field(repr=False, compare=False)
    _decision_output: DecisionOutput = field(default_factory=DecisionOutput, repr=False, compare=False)
    _skill_selection_cache: dict[Any, Any] = field(default_factory=dict, repr=False, compare=False)

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
                "Harnest context is available only during a managed invocation. "
                "This invocation has finished, so its session identity and capabilities "
                "can no longer be used. Await context-dependent work before the "
                "invocation ends; do not retain its context for later tasks."
            )


_ACTIVE_CONTEXT: ContextVar[AgentContext | None] = ContextVar(
    "harnest_agent_context", default=None
)


def optional_active_context() -> AgentContext | None:
    """Allow genuinely unmanaged calls without accepting revoked managed authority."""
    active = _ACTIVE_CONTEXT.get()
    if active is not None:
        active._require_active()
    return active


class _ContextAccess:
    """Decorate providers and access the context active in the current task."""

    def provider(
        self, name: str, *, order: int = 0
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Declare one invocation-scoped resource provider."""

        _validate_name(name)
        if not isinstance(order, int) or isinstance(order, bool):
            raise TypeError("context provider order must be an integer")

        def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
            if not callable(function):
                raise TypeError("@context.provider can only decorate callables")
            if getattr(function, "__harnest_tool__", False):
                raise TypeError("tools cannot also be context providers")
            if hasattr(function, _CONTEXT_ATTRIBUTE):
                raise TypeError("a function may provide only one context resource")
            setattr(function, _CONTEXT_ATTRIBUTE, ContextRegistration(name, order))
            return function

        return decorate

    def current(self) -> AgentContext:
        """Return the active invocation or fail outside managed execution."""

        active = optional_active_context()
        if active is None:
            raise ContextUnavailableError(
                "Harnest context is available only during a managed invocation. "
                "No active invocation was found for this tool or callback. Session "
                "identity, skills, and other managed capabilities require Harnest's "
                "runtime or evaluation entrypoint; they are not available during "
                "module import or an unwrapped native-framework call. If this happens "
                "inside a Harnest-managed run, report a context integration bug."
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
    def memory(self) -> "MemoryContext":
        """Return explicit cross-session memory bound to this invocation's owner."""
        from .context_memory import MemoryContext
        return MemoryContext(self.current())

    @property
    def decisions(self) -> "DecisionContext":
        """Evaluate explicitly registered decisions within the active invocation."""
        from .decision_runtime import DecisionContext

        return DecisionContext(self.current())

    @property
    def mcp(self) -> Any:
        """Return governed MCP access when the runtime installed a dispatcher."""

        self.current()
        from .mcp_context import mcp

        return mcp

    @property
    def extensions(self) -> Any:
        """Resolve typed Harnest Extension context within a managed invocation."""

        self.current()
        from .extension_runtime_context import extensions

        return extensions

    @property
    def skills(self) -> Any:
        """Return progressive skills scoped to the currently executing agent."""

        active = self.current()
        return active._skill_registry.access(active)

    @property
    def sandboxes(self) -> Any:
        """Return only sandbox grants assigned to the currently executing agent."""
        active = self.current()
        return active._sandbox_registry.access(active)

    @property
    def agent(self) -> Any:
        """Return task-scoped access to the compiled root agent runtime."""

        # A separate binding distinguishes durable task authority from ordinary
        # invocation context, where recursive root calls are not implicitly safe.
        from .context_agent import agent

        return agent

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
    memory_store: Any = None,
    memory_application_id: str | None = None,
    skill_registry: Any | None = None,
    sandbox_registry: Any | None = None,
    extension_bindings: Mapping[str, Any] | None = None,
) -> AgentContext:
    """Create a context with a private mutable registry for provider binding."""

    for name in resources:
        _validate_name(name)
    registry = dict(resources)
    from .extension_runtime_context import validate_extension_bindings

    extensions = validate_extension_bindings(
        {} if extension_bindings is None else extension_bindings
    )
    from .skills import SkillRegistry

    skills = SkillRegistry() if skill_registry is None else skill_registry
    if not isinstance(skills, SkillRegistry):
        raise TypeError("agent context skill_registry must be SkillRegistry")
    from .context_sandboxes import SandboxRegistry

    sandboxes = SandboxRegistry() if sandbox_registry is None else sandbox_registry
    if not isinstance(sandboxes, SandboxRegistry):
        raise TypeError("agent context sandbox_registry must be SandboxRegistry")
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
        _memory_store=memory_store,
        _memory_application_id=agent_name if memory_application_id is None else memory_application_id,
        _skill_registry=skills,
        _sandbox_registry=sandboxes,
        _skill_pins={},
        _extension_bindings=extensions,
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
        _memory_store=active._memory_store,
        # SubAgent display names must not change the cross-session owner scope.
        _memory_application_id=active._memory_application_id,
        _skill_registry=active._skill_registry,
        _sandbox_registry=active._sandbox_registry,
        _skill_pins=active._skill_pins,
        _skill_selection_cache=active._skill_selection_cache,
        _extension_bindings=active._extension_bindings,
        _lifetime=active._lifetime,
        # Child decisions belong to the same invocation output, with their own attribution.
        _decision_output=active._decision_output,
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
    if active._extension_bindings:
        from .extension_runtime_context import revoke_extension_bindings

        revoke_extension_bindings(active._extension_bindings)


@contextmanager
def activate_context(active: AgentContext) -> Iterator[None]:
    """Bind one invocation to the current async task and restore nesting safely."""

    active._require_active()
    token = _ACTIVE_CONTEXT.set(active)
    try:
        if not active._extension_bindings:
            # Empty extension bindings cannot change singleton context;
            # avoiding another generator context manager keeps the core path lean.
            yield
            return
        from .extension_runtime_context import activate_extension_bindings

        with activate_extension_bindings(active._extension_bindings):
            yield
    finally:
        _ACTIVE_CONTEXT.reset(token)


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not _CONTEXT_NAME.fullmatch(name):
        raise ValueError(
            "context resource name must be a valid public Python identifier"
        )


_access = _ContextAccess()


def provider(
    name: str, *, order: int = 0
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare one invocation-scoped resource on the public context namespace."""

    return _access.provider(name, order=order)


def current() -> AgentContext:
    """Return the active managed invocation context."""

    return _access.current()


def resource(name: str, expected_type: type[Any] | None = None) -> Any:
    """Resolve one explicitly published invocation resource."""

    return _access.resource(name, expected_type)


# These contracts depend on context activation primitives. Resolve them after
# module initialization so public imports do not create provider import cycles.
_PUBLIC_CONTRACTS = {
    "SessionContext": "context_session", "SessionDataError": "context_session",
    "StorageContext": "context_storage", "ScopedAssets": "context_assets",
    "MemoryContext": "context_memory",
    **dict.fromkeys((
        "AgentContinuationUnsupportedError", "AgentInvocationTimeout",
        "AgentInvocationUnavailableError", "AgentPendingResponse", "AgentResponse",
        "AgentSession", "AgentSessionNotFoundError", "AgentStreamItem", "LocalAgentRuntime",
    ), "context_agent"),
}
_ACCESS_MEMBERS = frozenset(
    {
        "agent",
        "agent_name",
        "assets",
        "credentials",
        "depth",
        "decisions",
        "extensions",
        "framework",
        "invocation_id",
        "is_root",
        "mcp",
        "metadata",
        "memory",
        "parent_agent_name",
        "sandboxes",
        "session",
        "session_id",
        "skills",
        "storage",
        "user_id",
    }
)


def __getattr__(name: str) -> Any:
    """Expose lazy contracts and values from the active invocation namespace."""
    from importlib import import_module

    module = _PUBLIC_CONTRACTS.get(name)
    if module is not None:
        value = getattr(import_module(f".{module}", __package__), name)
        globals()[name] = value
        return value
    if name in _ACCESS_MEMBERS:
        # Invocation values must be resolved per access; caching them on the
        # module would leak authority and identity across concurrent requests.
        return getattr(_access, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include lazy public contracts in interactive API discovery."""
    return sorted(set(globals()) | _PUBLIC_CONTRACTS.keys() | _ACCESS_MEMBERS)


__all__ = [
    "AgentContext",
    "ContextRegistration",
    "ContextResourceError",
    "ContextUnavailableError",
    "activate_agent_scope",
    "current",
    "derive_agent_context",
    "provider",
    "resource",
] + list(_PUBLIC_CONTRACTS)
