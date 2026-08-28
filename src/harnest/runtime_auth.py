"""Authentication injection for standalone Harnest HTTP runtimes."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Protocol, runtime_checkable


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

    def __post_init__(self) -> None:
        if not isinstance(self.user_id, str) or not self.user_id.strip():
            raise ValueError("authenticated user_id must be a non-empty string")
        if not isinstance(self.claims, Mapping):
            raise TypeError("authenticated claims must be a mapping")
        object.__setattr__(self, "claims", MappingProxyType(dict(self.claims)))


@dataclass(frozen=True, slots=True)
class ConnectionContext:
    """Framework-independent, read-only connection data for authentication."""

    transport: str
    method: str | None
    path: str
    headers: Mapping[str, str]
    cookies: Mapping[str, str]
    query: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.transport not in {"http", "websocket"}:
            raise ValueError("connection transport must be http or websocket")
        object.__setattr__(self, "headers", _readonly_strings(self.headers))
        object.__setattr__(self, "cookies", _readonly_strings(self.cookies))
        object.__setattr__(self, "query", _readonly_strings(self.query))


class AuthenticationError(RuntimeError):
    """A request did not satisfy the deployment authentication policy."""

    def __init__(self, detail: str = "Authentication required", *, status_code: int = 401):
        if status_code not in {401, 403}:
            raise ValueError("authentication status_code must be 401 or 403")
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@runtime_checkable
class Authenticator(Protocol):
    """Resolve one HTTP or WebSocket connection to a verified principal."""

    async def authenticate(self, connection: Any) -> AuthPrincipal: ...


class AnonymousAuthenticator:
    """Development default that preserves the local single-user behavior."""

    async def authenticate(self, connection: Any) -> AuthPrincipal:
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
        await self._app(scope, receive, send)


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
