"""Invocation-owned Agent Runtime Principal permission projection."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import re
from typing import Any
import uuid


_PERMISSION = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{0,127}$")
_PERMISSIONS_ATTRIBUTE = "__harnest_required_permissions__"


class AgentRuntimePermissionError(PermissionError):
    """A capability is unavailable to the active Agent Runtime Principal."""


@dataclass(frozen=True, slots=True)
class AgentRuntimePrincipal:
    """Identify one invocation and the permission identifiers it carries."""

    permissions: frozenset[str] = field(default_factory=frozenset)
    id: str = field(default_factory=lambda: f"arp_{uuid.uuid4().hex}", init=False)

    def __post_init__(self) -> None:
        """Freeze grants and keep the generated identity opaque."""

        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("Agent Runtime Principal id must be non-empty")
        if isinstance(self.permissions, (str, bytes)) or not isinstance(
            self.permissions, (Sequence, set, frozenset)
        ):
            raise TypeError("Agent Runtime Principal permissions must be a collection")
        normalized = frozenset(self.permissions)
        for permission in normalized:
            validate_permission(permission)
        object.__setattr__(self, "permissions", normalized)

    @classmethod
    def create(
        cls, *, permissions: Sequence[str] | set[str] | frozenset[str] = ()
    ) -> AgentRuntimePrincipal:
        """Create an opaque runtime identity from application-authorized grants."""

        return cls(frozenset(permissions))

    def restrict(
        self, permissions: Sequence[str] | set[str] | frozenset[str]
    ) -> AgentRuntimePrincipal:
        """Create a fresh principal without permitting authority amplification."""

        selected = frozenset(permissions)
        unavailable = selected - self.permissions
        if unavailable:
            raise AgentRuntimePermissionError(
                "a derived Agent Runtime Principal cannot add permissions"
            )
        return type(self).create(permissions=selected)


@dataclass(slots=True)
class _PrincipalLifetime:
    """Revoke private bindings copied into work that outlives an invocation."""

    active: bool = True


@dataclass(frozen=True, slots=True)
class _PrincipalBinding:
    """Keep an optional principal distinct from the public Harnest context."""

    principal: AgentRuntimePrincipal | None
    lifetime: _PrincipalLifetime = field(repr=False)


_ACTIVE_AGENT_PRINCIPAL: ContextVar[_PrincipalBinding | None] = ContextVar(
    "harnest_agent_runtime_principal", default=None
)


def create_agent_principal_binding(
    principal: AgentRuntimePrincipal | None,
) -> _PrincipalBinding:
    """Create one revocable binding reused while a stream yields to its caller."""

    validate_agent_principal(principal)
    return _PrincipalBinding(principal, _PrincipalLifetime())


@contextmanager
def activate_agent_principal(binding: _PrincipalBinding) -> Iterator[None]:
    """Bind invocation permission projection without exposing it to agent code."""

    if not isinstance(binding, _PrincipalBinding) or not binding.lifetime.active:
        raise AgentRuntimePermissionError("Agent Runtime Principal scope is inactive")
    token = _ACTIVE_AGENT_PRINCIPAL.set(binding)
    try:
        yield
    finally:
        _ACTIVE_AGENT_PRINCIPAL.reset(token)


def revoke_agent_principal(binding: _PrincipalBinding) -> None:
    """Invalidate bindings copied into tasks after their invocation finishes."""

    binding.lifetime.active = False


def active_agent_principal() -> AgentRuntimePrincipal | None:
    """Return only a live private runtime binding; absence means compatibility mode."""

    binding = _ACTIVE_AGENT_PRINCIPAL.get()
    if binding is None:
        return None
    if not binding.lifetime.active:
        raise AgentRuntimePermissionError("Agent Runtime Principal scope is inactive")
    return binding.principal


def validate_agent_principal(value: Any) -> None:
    """Reject lookalike values before they reach framework-specific adapters."""

    if value is not None and not isinstance(value, AgentRuntimePrincipal):
        raise TypeError("agent_principal must be an AgentRuntimePrincipal or None")


def resolve_nested_agent_principal(
    requested: AgentRuntimePrincipal | None,
) -> AgentRuntimePrincipal | None:
    """Inherit private authority and prevent nested invocation amplification."""

    validate_agent_principal(requested)
    binding = _ACTIVE_AGENT_PRINCIPAL.get()
    if binding is None:
        return requested
    active = active_agent_principal()
    if active is None:
        return requested
    if requested is None:
        return active
    if requested.permissions <= active.permissions:
        return requested
    raise AgentRuntimePermissionError(
        "a nested Agent Runtime Principal cannot add permissions"
    )


def validate_permission(value: Any) -> str:
    """Require stable permission identifiers suitable for tool metadata."""

    if not isinstance(value, str) or _PERMISSION.fullmatch(value) is None:
        raise ValueError(
            "permission must start with a letter and contain only letters, "
            "numbers, '.', '_', ':', or '-'"
        )
    return value


def attach_required_permissions(value: Any, permissions: Sequence[str]) -> Any:
    """Attach immutable permission metadata to one framework capability."""

    normalized = frozenset(validate_permission(item) for item in permissions)
    try:
        setattr(value, _PERMISSIONS_ATTRIBUTE, normalized)
    except (AttributeError, TypeError):
        object.__setattr__(value, _PERMISSIONS_ATTRIBUTE, normalized)
    return value


def required_permissions(value: Any) -> frozenset[str]:
    """Resolve metadata across Harnest callables and native tool adapters."""

    for candidate in (
        value,
        getattr(value, "func", None),
        getattr(value, "coroutine", None),
    ):
        permissions = getattr(candidate, _PERMISSIONS_ATTRIBUTE, None)
        if isinstance(permissions, frozenset):
            return permissions
    return frozenset()


def capability_is_available(value: Any) -> bool:
    """Apply permissioned-tool requirements to the active invocation principal."""

    return permissions_are_available(required_permissions(value))


def permissions_are_available(required: Sequence[str] | frozenset[str]) -> bool:
    """Apply one normalized requirement set to the private active principal."""

    required_set = frozenset(required)
    principal = active_agent_principal()
    return not required_set or principal is None or required_set <= principal.permissions


def require_capability(value: Any, *, name: str) -> None:
    """Fail closed when a hidden capability reaches an execution boundary."""

    if not capability_is_available(value):
        raise AgentRuntimePermissionError(
            f"capability {name!r} is unavailable to the Agent Runtime Principal"
        )


__all__ = ["AgentRuntimePermissionError", "AgentRuntimePrincipal"]
