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
    _resources: Mapping[str, Any] = field(repr=False)
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
) -> AgentContext:
    """Create a context with a private mutable registry for provider binding."""

    for name in resources:
        _validate_name(name)
    registry = dict(resources)
    return AgentContext(
        framework=framework,
        agent_name=agent_name,
        invocation_id=invocation_id,
        user_id=user_id,
        session_id=session_id,
        metadata=MappingProxyType(dict(metadata)),
        _resources=MappingProxyType(registry),
        _lifetime=_ContextLifetime(),
    )


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


@contextmanager
def activate_context(active: AgentContext) -> Iterator[None]:
    """Bind one invocation to the current async task and restore nesting safely."""

    active._require_active()
    token = _ACTIVE_CONTEXT.set(active)
    try:
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
    "context",
]
