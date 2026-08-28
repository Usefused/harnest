"""Invocation-scoped credential resolution without agent-visible storage."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .context import context

if TYPE_CHECKING:
    from .runtime_auth import AuthPrincipal


class CredentialError(RuntimeError):
    """Base class for safe credential-resolution failures."""


class CredentialUnavailableError(CredentialError):
    """Raised when an invocation cannot obtain a requested credential."""


class CredentialProviderError(CredentialError):
    """Raised when a configured provider fails or violates its contract."""


@dataclass(frozen=True, slots=True)
class CredentialRequest:
    """Authenticated principal and authorization scope for one resolution."""

    audience: str
    scopes: tuple[str, ...]
    framework: str
    agent_name: str
    invocation_id: str
    session_id: str
    principal: AuthPrincipal = field(repr=False)

    def __post_init__(self) -> None:
        """Validate identity fields and canonicalize scopes for providers."""

        for name in (
            "audience",
            "framework",
            "agent_name",
            "invocation_id",
            "session_id",
        ):
            _require_text(getattr(self, name), name)
        from .runtime_auth import AuthPrincipal

        if not isinstance(self.principal, AuthPrincipal):
            raise TypeError("credential principal must be AuthPrincipal")
        object.__setattr__(self, "scopes", _normalize_scopes(self.scopes))


@dataclass(frozen=True, slots=True)
class Credential:
    """Opaque material Harnest passes transiently and never serializes.

    Material may be a token or a framework-native credential value. Harnest
    keeps either form redacted and requires transport adapters to reveal it at
    the final outbound security boundary.
    """

    _material: Any = field(repr=False)

    def __post_init__(self) -> None:
        """Reject an absent value so providers cannot return ambiguous credentials."""

        if self._material is None:
            raise ValueError("credential material cannot be None")

    def reveal(self) -> Any:
        """Return credential material for transport adapters at the security boundary."""

        return self._material

    def __repr__(self) -> str:
        return "Credential(<redacted>)"

    def __str__(self) -> str:
        return "<redacted credential>"


class CredentialProvider(ABC):
    """Application-owned source of short-lived invocation credentials."""

    async def start(self) -> None:
        """Initialize provider-owned clients before accepting invocations."""

    @abstractmethod
    async def resolve(self, request: CredentialRequest) -> Credential | None:
        """Resolve a credential for one invocation and downstream audience."""

    async def close(self) -> None:
        """Release provider-owned clients after invocations have drained."""


@dataclass(slots=True)
class _ProviderLifetime:
    """Revoke a binding in child tasks that copied its context variable."""

    active: bool = True


@dataclass(frozen=True, slots=True)
class _ProviderBinding:
    """Keep the provider private while sharing invocation revocation state."""

    provider: CredentialProvider = field(repr=False)
    lifetime: _ProviderLifetime = field(repr=False)


_ACTIVE_PROVIDER: ContextVar[_ProviderBinding | None] = ContextVar(
    "harnest_credential_provider", default=None
)


class _CredentialAccess:
    """Resolve credentials from the provider bound to the current invocation."""

    async def resolve(
        self, audience: str, scopes: Iterable[str] = ()
    ) -> Credential:
        """Resolve opaque material using only private invocation identity."""

        active = context.current()
        principal = _credential_principal(active.user_id)
        request = CredentialRequest(
            audience=audience,
            scopes=_normalize_scopes(scopes),
            framework=active.framework,
            agent_name=active.agent_name,
            invocation_id=active.invocation_id,
            session_id=active.session_id,
            principal=principal,
        )
        binding = _active_binding()
        result, failure = await _resolve_safely(binding.provider, request)
        if failure is not None:
            # Raise outside the provider exception handler so secret-bearing
            # exceptions cannot remain reachable through __context__.
            raise failure
        if result is None:
            raise CredentialUnavailableError(
                "credential provider did not return a credential"
            )
        if not isinstance(result, Credential):
            raise CredentialProviderError(
                "credential provider resolve must return Credential; "
                f"got {type(result).__name__}"
            )
        return result


credentials = _CredentialAccess()


def _credential_principal(invocation_user_id: str) -> AuthPrincipal:
    """Use verified request identity or create a credential-free direct principal."""

    from .runtime_auth import AuthPrincipal, _active_authenticated_principal

    principal = _active_authenticated_principal()
    if principal is None:
        return AuthPrincipal(invocation_user_id)
    if principal.user_id != invocation_user_id:
        # Invocation identity must never diverge from the principal whose
        # private credentials authorize its downstream calls.
        raise CredentialProviderError(
            "authenticated principal does not match invocation identity"
        )
    return principal


def _active_binding() -> _ProviderBinding:
    """Return an active private provider or fail closed after revocation."""

    binding = _ACTIVE_PROVIDER.get()
    if binding is None or not binding.lifetime.active:
        raise CredentialUnavailableError(
            "no credential provider is active for this invocation"
        )
    return binding


async def _resolve_safely(
    provider: CredentialProvider, request: CredentialRequest
) -> tuple[Any, BaseException | None]:
    """Resolve while detaching every secret-bearing provider exception."""

    try:
        return await provider.resolve(request), None
    except asyncio.CancelledError:
        # Cancellation retains task-control semantics but not a provider's
        # potentially sensitive custom message or exception chain.
        failure: BaseException = asyncio.CancelledError()
    except Exception as error:
        failure = CredentialProviderError(
            "credential provider resolve failed with " f"{type(error).__name__}"
        )
    return None, failure


@contextmanager
def _activate_credential_provider(provider: CredentialProvider) -> Iterator[None]:
    """Bind a provider privately and revoke copied task bindings on exit."""

    if not isinstance(provider, CredentialProvider):
        raise TypeError("credential provider must implement CredentialProvider")
    lifetime = _ProviderLifetime()
    token = _ACTIVE_PROVIDER.set(_ProviderBinding(provider, lifetime))
    try:
        yield
    finally:
        # ContextVars are copied into child tasks, so resetting only this task
        # would otherwise let late work continue resolving credentials.
        lifetime.active = False
        _ACTIVE_PROVIDER.reset(token)


def _normalize_scopes(scopes: Iterable[str]) -> tuple[str, ...]:
    """Return trimmed, de-duplicated scopes in stable caller order."""

    if isinstance(scopes, (str, bytes)):
        raise TypeError("credential scopes must be an iterable of strings")
    try:
        values = tuple(scopes)
    except TypeError:
        raise TypeError("credential scopes must be an iterable of strings") from None
    normalized: list[str] = []
    for value in values:
        _require_text(value, "scope")
        scope = value.strip()
        if scope not in normalized:
            normalized.append(scope)
    return tuple(normalized)


def _require_text(value: Any, name: str) -> None:
    """Require text identifiers without coercing application-owned values."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"credential {name} must be a non-empty string")


__all__ = [
    "Credential",
    "CredentialError",
    "CredentialProvider",
    "CredentialProviderError",
    "CredentialRequest",
    "CredentialUnavailableError",
    "credentials",
]
