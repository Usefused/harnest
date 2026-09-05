"""Authentication injection for standalone Harnest HTTP runtimes."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Protocol, runtime_checkable

from .credentials import Credential


ANONYMOUS_USER_ID = "_harnest_neutral"
_PRINCIPAL_STATE_KEY = "harnest_principal"
_PUBLIC_PATHS = frozenset(
    {
        "/",
        "/_harnest/playground.css",
        "/_harnest/playground.js",
        "/healthz",
        "/.well-known/agent-card.json",
        "/agent",
    }
)


@dataclass(frozen=True, slots=True)
class AuthPrincipal:
    """A verified tenant/user identity supplied by a deployment authenticator."""

    user_id: str
    claims: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    credentials: Mapping[str, Credential] = field(
        default_factory=lambda: MappingProxyType({}), repr=False
    )

    def __post_init__(self) -> None:
        """Freeze selected claims and require opaque named credentials."""

        if not isinstance(self.user_id, str) or not self.user_id.strip():
            raise ValueError("authenticated user_id must be a non-empty string")
        if not isinstance(self.claims, Mapping):
            raise TypeError("authenticated claims must be a mapping")
        if not isinstance(self.credentials, Mapping):
            raise TypeError("authenticated credentials must be a mapping")
        for name, credential in self.credentials.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("authenticated credential names must be non-empty strings")
            if not isinstance(credential, Credential):
                raise TypeError(
                    "authenticated credential values must be Credential instances"
                )
        object.__setattr__(self, "claims", MappingProxyType(dict(self.claims)))
        object.__setattr__(
            self, "credentials", MappingProxyType(dict(self.credentials))
        )


@dataclass(slots=True)
class _PrincipalLifetime:
    """Revoke authenticated identity in tasks copied from a finished request."""

    active: bool = True


@dataclass(frozen=True, slots=True)
class _PrincipalBinding:
    """Carry verified identity privately without exposing claims to agents."""

    principal: AuthPrincipal = field(repr=False)
    lifetime: _PrincipalLifetime = field(repr=False)


_ACTIVE_PRINCIPAL: ContextVar[_PrincipalBinding | None] = ContextVar(
    "harnest_authenticated_principal", default=None
)


@dataclass(frozen=True, slots=True)
class ConnectionContext:
    """Framework-independent, read-only connection data for authentication."""

    transport: str
    method: str | None
    path: str
    headers: Mapping[str, str] = field(repr=False)
    cookies: Mapping[str, str] = field(repr=False)
    query: Mapping[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        if self.transport not in {"http", "websocket"}:
            raise ValueError("connection transport must be http or websocket")
        object.__setattr__(self, "headers", _readonly_strings(self.headers))
        object.__setattr__(self, "cookies", _readonly_strings(self.cookies))
        object.__setattr__(self, "query", _readonly_strings(self.query))


class AuthenticationError(RuntimeError):
    """A request did not satisfy the deployment authentication policy."""

    def __init__(
        self,
        detail: str = "Authentication required",
        *,
        status_code: int = 401,
    ) -> None:
        if status_code not in {401, 403}:
            raise ValueError("authentication status_code must be 401 or 403")
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@runtime_checkable
class Authenticator(Protocol):
    """Resolve one HTTP or WebSocket connection to a verified principal."""

    async def authenticate(self, connection: Any) -> AuthPrincipal:
        """Return the authenticated principal for one incoming connection."""

        ...


class AnonymousAuthenticator:
    """Development default that preserves the local single-user behavior."""

    async def authenticate(self, connection: Any) -> AuthPrincipal:
        """Return the built-in anonymous principal for every connection."""

        del connection
        return AuthPrincipal(ANONYMOUS_USER_ID)


def principal_for(connection: Any) -> AuthPrincipal:
    """Read the principal installed before a protected route executes."""

    principal = getattr(connection.state, _PRINCIPAL_STATE_KEY, None)
    return principal if isinstance(principal, AuthPrincipal) else AuthPrincipal(
        ANONYMOUS_USER_ID
    )


def install_authentication(app: Any, authenticator: Authenticator | None) -> None:
    """Protect execution/session routes while leaving discovery public."""

    if authenticator is None:
        return
    if not isinstance(authenticator, Authenticator):
        raise TypeError("authenticator must implement authenticate(connection)")
    app.add_middleware(_AuthenticationMiddleware, authenticator=authenticator)


class _AuthenticationMiddleware:
    def __init__(self, app: Any, *, authenticator: Authenticator) -> None:
        self._app = app
        self._authenticator = authenticator

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if not _requires_authentication(scope):
            await self._app(scope, receive, send)
            return
        try:
            principal = await _authenticate_scope(scope, self._authenticator)
        except AuthenticationError as exc:
            await _reject(scope, receive, send, exc)
            return
        scope.setdefault("state", {})[_PRINCIPAL_STATE_KEY] = principal
        # Native framework routes can accept caller-authored user fields. Keep
        # the verified principal on a separate private channel so downstream
        # credential policy never treats those fields as authentication.
        with _activate_authenticated_principal(principal):
            await self._app(scope, receive, send)


@contextmanager
def _activate_authenticated_principal(
    principal: AuthPrincipal,
) -> Iterator[None]:
    """Bind verified identity for one request and revoke copied task contexts."""

    if not isinstance(principal, AuthPrincipal):
        raise TypeError("authenticated principal must be AuthPrincipal")
    lifetime = _PrincipalLifetime()
    token = _ACTIVE_PRINCIPAL.set(_PrincipalBinding(principal, lifetime))
    try:
        yield
    finally:
        # ContextVars are copied into framework-created tasks; explicit
        # revocation prevents late work from retaining request authority.
        lifetime.active = False
        _ACTIVE_PRINCIPAL.reset(token)


def _active_authenticated_principal() -> AuthPrincipal | None:
    """Return the verified request principal without manufacturing a fallback."""

    binding = _ACTIVE_PRINCIPAL.get()
    if binding is None or not binding.lifetime.active:
        return None
    return binding.principal


@contextmanager
def _activate_task_principal(principal: AuthPrincipal) -> Iterator[None]:
    """Grant one explicitly requested background task its own authority lifetime.

    HTTP middleware revokes copied request bindings when a response completes.
    Protocols such as A2A can deliberately return a Task before execution ends,
    so that execution needs a separate binding whose lifetime is owned by the
    task rather than accidentally inherited from the request.
    """

    if not isinstance(principal, AuthPrincipal):
        raise TypeError("task principal must be AuthPrincipal")
    lifetime = _PrincipalLifetime()
    token = _ACTIVE_PRINCIPAL.set(_PrincipalBinding(principal, lifetime))
    try:
        yield
    finally:
        lifetime.active = False
        _ACTIVE_PRINCIPAL.reset(token)


def _requires_authentication(scope: Mapping[str, Any]) -> bool:
    return scope.get("type") in {"http", "websocket"} and scope.get("path") not in _PUBLIC_PATHS


async def _authenticate_scope(
    scope: dict[str, Any], authenticator: Authenticator
) -> AuthPrincipal:
    from starlette.requests import HTTPConnection

    principal = await authenticator.authenticate(_connection_context(HTTPConnection(scope)))
    if not isinstance(principal, AuthPrincipal):
        raise TypeError("authenticator must return AuthPrincipal")
    return principal


def _connection_context(connection: Any) -> ConnectionContext:
    return ConnectionContext(
        transport=connection.scope["type"],
        method=connection.scope.get("method"),
        path=connection.url.path,
        headers=dict(connection.headers),
        cookies=dict(connection.cookies),
        query=dict(connection.query_params),
    )


def _readonly_strings(values: Mapping[str, Any]) -> Mapping[str, str]:
    if not isinstance(values, Mapping):
        raise TypeError("connection context values must be mappings")
    return MappingProxyType({str(key): str(value) for key, value in values.items()})


async def _reject(
    scope: Mapping[str, Any], receive: Any, send: Any, error: AuthenticationError
) -> None:
    if scope.get("type") == "websocket":
        await send({"type": "websocket.close", "code": 4401 if error.status_code == 401 else 4403})
        return
    from starlette.responses import JSONResponse

    response = JSONResponse({"detail": error.detail}, status_code=error.status_code)
    await response(scope, receive, send)


__all__ = [
    "ANONYMOUS_USER_ID",
    "AuthPrincipal",
    "AuthenticationError",
    "Authenticator",
    "AnonymousAuthenticator",
    "ConnectionContext",
    "install_authentication",
    "principal_for",
]
