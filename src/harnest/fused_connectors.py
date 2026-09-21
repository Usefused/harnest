"""Shared Fused OAuth and MCP management backend for trusted local Harnest hosts."""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Sequence
from starlette.requests import Request
from ._fused_admin_client import AppTokenPayload, FusedAdminClient, FusedAdminConfig, FusedAdminError
from ._fused_auth_client import FusedAuthClient, FusedAuthConfig

_OAUTH_CALLBACK_PATH = "/_harnest/connectors/oauth/callback"
# After a successful OAuth callback the Studio returns to the MCP connections
# view, which is where the connect action originates.
_MCP_VIEW_URL = "/?view=studio&studioView=connections"
# Scopes the Studio needs: list, deploy, and mint tokens for MCP servers. The
# Engine intersects every delegated grant with these, so a missing scope denies
# the matching management call instead of silently widening it.
# Deployment also authorizes the workspace, the target bucket, and each bound
# service, so the Studio must request those capabilities alongside app access.
_DEFAULT_SCOPES = (
    "app.read", "app.create", "app.manage", "app.tokens.manage",
    "service.read", "service.consume",
    "workspace.read",
    "bucket.use",
    "catalogue.read",
)
_SERVER_LIST_LIMIT = 200  # One bounded read resolves both list and version pickers.
# Studio-minted execution tokens are temporary; a short, bounded lifetime lets a
# developer reuse the same name once a previous token expires.
_TOKEN_TTL_SECONDS = 7 * 24 * 3600


class ConnectorError(RuntimeError):
    """A failed or unconfigured Fused Auth/Admin call; never carries raw secrets."""


@dataclass(frozen=True, slots=True)
class ConnectorResult:
    """A resolved connection, its env-var name, and the one-time execution token.

    ``url`` is operational metadata and safe to display, store, or embed; ``token``
    is a credential shown exactly once and never persisted by this process.
    """

    url: str
    token_env: str
    token: str

    name: str = ""


@dataclass(slots=True)
class _Session:
    """A live OAuth session: the access token plus its absolute expiry."""

    access_token: str
    refresh_token: str | None
    expires_at: float
    client: Any = None


def _env(name: str) -> str | None:
    """Read a non-empty environment value, treating blank input as absent."""

    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


class FusedConnectorsService:
    """Authenticate into Fused and manage MCP servers through the Admin client."""

    def __init__(
        self,
        *,
        engine_url: str | None = None,
        scopes: Sequence[str] = _DEFAULT_SCOPES,
    ) -> None:
        """Configure Engine discovery and keep OAuth credentials only in memory."""

        # Explicit arguments win; environment supplies a server-side default so
        # a `harnest serve` process can be configured without touching code.
        # Temporary client credentials are requested at connect time, so the
        # Engine URL is the only required configuration.
        self.engine_url = (engine_url or _env("HARNEST_FUSED_ENGINE_URL") or "").rstrip("/")
        scopes_env = _env("HARNEST_FUSED_OAUTH_SCOPES")
        # Explicit environment scopes override the built-in development defaults.
        self.scopes = tuple(scopes_env.split(",")) if scopes_env else tuple(scopes)
        # OAuth state is in-memory by design: the playground is a single-process
        # local development surface, so no persistence or cross-process sharing
        # is required and no token material is ever written to disk.
        self._lock = threading.Lock()
        self._flows: dict[str, tuple] = {}
        self._sessions: dict[str, _Session] = {}

    def available(self) -> bool:
        """Connecting needs only the Engine URL; the user authenticates in the browser."""

        return bool(self.engine_url)

    def connected(self, session_id: str | None) -> bool:
        """True when a live access token is bound to the browser session."""

        session = self._sessions.get(session_id or "")
        return bool(session and (session.expires_at > time.time() or session.refresh_token))

    def redirect_uri(self, request: Request) -> str:
        """Resolve the callback URL the Engine redirects back to after consent.

        Derived from the served origin so it needs no configuration; this exact
        URL is what the developer registers on their public OAuth client.
        """

        return f"{request.url.scheme}://{request.url.netloc}{_OAUTH_CALLBACK_PATH}"

    def authorize_url(self, redirect_uri: str, session_id: str = "") -> str:
        """Request temporary credentials, then begin user login with a PKCE pair."""

        client_id, client_secret = self._register_client(redirect_uri, "Harnest Studio")
        client = self._auth_client(redirect_uri, client_id, client_secret)
        state = secrets.token_urlsafe(24)
        request = client.authorize_url(scopes=list(self.scopes), state=state)
        # The verifier is the proof-of-possession half of PKCE and must never
        # be exposed; only the state travels in the browser URL. The client id is
        # kept with its secret for the callback; neither secret enters the URL.
        with self._lock:
            self._flows = {key: value for key, value in self._flows.items() if value[5] > time.time()}
            if len(self._flows) >= 32:
                raise ConnectorError("Too many pending Fused logins")
            self._flows[state] = (client_id, client_secret, request.code_verifier, session_id, redirect_uri, time.time() + 600)
        return request.url

    def complete_authorization(self, code: str, state: str, redirect_uri: str, session_id: str) -> None:
        """Exchange an authorization code and bind the resulting tokens to the session."""

        with self._lock:
            flow = self._flows.pop(state, None)
        # A missing flow means the state is forged, stale, or already used;
        # failing closed prevents a replayed callback from minting a session.
        if flow is None:
            raise ConnectorError("OAuth state is missing or already consumed")
        client_id, client_secret, verifier, browser, callback, expires = flow
        if expires <= time.time() or callback != redirect_uri or (browser and browser != session_id):
            raise ConnectorError("OAuth flow expired or belongs to another browser")
        client = self._auth_client(redirect_uri, client_id, client_secret)
        tokens = client.exchange_code(code, verifier)
        with self._lock:
            self._sessions[session_id] = _Session(
                access_token=tokens.access_token,
                refresh_token=tokens.refresh_token,
                expires_at=time.time() + max(tokens.expires_in, 1),
                client=client,
            )

    def disconnect(self, session_id: str | None) -> None:
        """Drop the bound session so a fresh consent can be started."""

        with self._lock:
            self._sessions.pop(session_id or "", None)
            self._flows = {key: flow for key, flow in self._flows.items() if flow[3] != session_id}

    def servers(self, session_id: str | None, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        """List every deployed MCP server version once, for both pickers."""

        client = self._admin_client(self._require_token(session_id))
        listed = client.list_servers(limit=limit, offset=offset)
        return {"items": [_server_dict(item) for item in listed.items], "total": listed.total}

    def versions(self, name: str, session_id: str | None, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        """List one server's immutable versions by filtering the flat listing."""

        listed = self.servers(session_id, limit=_SERVER_LIST_LIMIT, offset=0)
        matches = [item for item in listed["items"] if item["name"] == name]
        return {"items": matches[offset : offset + limit], "total": len(matches)}

    def services(self, session_id: str | None) -> dict[str, Any]:
        """List workspace services so a new MCP server can bind a subset of them."""

        client = self._admin_client(self._require_token(session_id))
        return {"items": client.list_services()}

    def operations(self, session_id: str | None, service_id: str, version: str) -> dict[str, Any]:
        """List one service's operations so a new MCP server can bind a subset of them."""

        client = self._admin_client(self._require_token(session_id))
        return {"items": client.list_service_operations(service_id, version)}

    def add_existing(self, session_id: str | None, *, name: str, version: str, token_name: str = "studio") -> ConnectorResult:
        """Resolve an already-deployed server's URL and mint its execution token."""

        url = self._pinned_url(session_id, name, version)
        token = self._generate_token(session_id, name, token_name)
        return ConnectorResult(name=name, url=url, token_env=_token_env(name), token=token)

    def create(self, session_id: str | None, *, name: str, description: str, services: Sequence[Any], token_name: str = "studio") -> ConnectorResult:
        """Deploy a new Engine-hosted MCP server from the Studio's structured fields."""

        if not services:
            raise ValueError("select at least one workspace service")
        client = self._admin_client(self._require_token(session_id))
        server = client.deploy_server(_mcp_config(name, description, services))
        url = _url_from_server(server)
        token = self._generate_token(session_id, server.name, token_name)
        return ConnectorResult(name=server.name, url=url, token_env=_token_env(server.name), token=token)

    def token(self, session_id: str | None, *, name: str, token_name: str = "studio") -> dict[str, Any]:
        """Mint a fresh execution token for an existing server by name."""

        return {"token": self._generate_token(session_id, name, token_name), "tokenEnv": _token_env(name)}

    def _require_token(self, session_id: str | None) -> str:
        """Return the live access token or a clear reconnect instruction."""

        session = self._sessions.get(session_id or "")
        if session is None or not session.access_token:
            raise ConnectorError("connect your Fused workspace first")
        if session.expires_at <= time.time():
            self._refresh(session)
        return session.access_token

    def _refresh(self, session: _Session) -> None:
        """Rotate an expired grant under the session lock; credentials never leave the host."""
        with self._lock:
            if session.expires_at > time.time():
                return
            if not session.refresh_token or not session.client:
                raise ConnectorError("Fused session expired; reconnect your workspace")
            try:
                tokens = session.client.refresh(session.refresh_token)
            except Exception:
                raise ConnectorError("Fused session expired; reconnect your workspace") from None
            session.access_token = tokens.access_token
            session.refresh_token = tokens.refresh_token or session.refresh_token
            session.expires_at = time.time() + max(tokens.expires_in, 1)

    def deploy(self, session_id: str, config: dict, owner_team: str | None = None) -> dict:
        """Deploy a reviewed configuration, exposing transport metadata rather than credentials."""
        client = self._admin_client(self._require_token(session_id))
        server = client.deploy_server(config, owner_team=owner_team) if owner_team else client.deploy_server(config)
        return {"name": server.name, "url": _url_from_server(server)}

    def resolve(self, session_id: str, name: str, version: str, offset: int = 0) -> str:
        """Resolve a selected immutable server without creating an execution token."""
        return self._pinned_url(session_id, name, version, offset=offset)

    def issue(self, session_id: str, name: str, token_name: str) -> str:
        """Mint an explicit new token; callers must retain its one-time value securely."""
        client = self._admin_client(self._require_token(session_id))
        return client.generate_token(name, AppTokenPayload(name=token_name, expires_in=_TOKEN_TTL_SECONDS)).token

    def _generate_token(self, session_id: str | None, name: str, token_name: str) -> str:
        """Mint one execution token; the Engine returns its plaintext exactly once.

        Returns an empty string when the environment already defines this
        server's token variable, since a previously minted token's plaintext is
        deliberately unrecoverable and the connection only needs to reference
        the variable.
        """

        # Reuse an already-configured token from the environment instead of
        # minting (and naming) a fresh credential on every connect.
        if _env(_token_env(name)):
            return ""
        client = self._admin_client(self._require_token(session_id))
        # Studio execution tokens are temporary: mint with a bounded lifetime so
        # the fixed label stays readable and becomes reusable once the token
        # expires (the Engine purges expired tokens before enforcing the name
        # uniqueness constraint).
        try:
            return client.generate_token(
                name, AppTokenPayload(name=token_name, expires_in=_TOKEN_TTL_SECONDS)
            ).token
        except FusedAdminError as exc:
            # A live token with this label already exists and its plaintext is
            # unrecoverable, so fall back to referencing the environment
            # variable the connection already expects.
            if exc.code == "app_token_name_conflict":
                return ""
            raise

    def _pinned_url(self, session_id: str | None, name: str, version: str, *, offset: int = 0) -> str:
        """Resolve one exact, active immutable version's endpoint."""

        matches = [
            item for item in self.servers(session_id, limit=_SERVER_LIST_LIMIT, offset=offset)["items"]
            if item["name"] == name and item["version"] == version
        ]
        active = [item for item in matches if item.get("active")]
        if len(active) != 1:
            raise ConnectorError(f"MCP version {name}@{version} is missing, ambiguous, or inactive")
        return _url_from_dict(active[0], name, version)

    def _auth_client(self, redirect_uri: str, client_id: str, client_secret: str) -> FusedAuthClient:
        """Keep the temporary secret server-side while also requiring PKCE."""

        return FusedAuthClient(FusedAuthConfig(
            issuer=self.engine_url,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
        ))

    def _register_client(self, redirect_uri: str, name: str) -> tuple[str, str]:
        """Request temporary client credentials before the user authenticates.

        Returns the one-time (client_id, client_secret); the client is
        loopback-bound and short-lived, and it is inert until the user consents.
        """

        body = json.dumps({
            "name": name,
            "redirect_uri": redirect_uri,
            "scopes": list(self.scopes),
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{self.engine_url}/oauth/register",
            data=body,
            headers={
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise ConnectorError(f"Fused OAuth client registration failed ({error.code})") from error
        client_id = payload.get("client_id")
        client_secret = payload.get("client_secret")
        # An incomplete pair cannot authenticate the subsequent token exchange.
        if not isinstance(client_id, str) or not client_id or not isinstance(client_secret, str) or not client_secret:
            raise ConnectorError("Fused OAuth registration returned incomplete client credentials")
        return client_id, client_secret

    def _admin_client(self, access_token: str) -> FusedAdminClient:
        """Construct the management client bound to the current access token."""

        return FusedAdminClient(FusedAdminConfig(engine_url=self.engine_url, access_token=access_token))


def _server_dict(item: Any) -> dict[str, Any]:
    """Project an Admin client server into the browser-facing wire shape."""

    urls = item.transport_urls
    return {
        "id": item.id,
        "name": item.name,
        "version": item.version,
        "active": bool(item.active),
        "stable": bool(item.stable),
        "transport_urls": {
            "streamable_http": urls.streamable_http,
            "sse": urls.sse,
            "versioned_streamable_http": urls.versioned_streamable_http,
            "versioned_sse": urls.versioned_sse,
        } if urls else None,
    }


def _url_from_dict(item: dict[str, Any], name: str, version: str) -> str:
    """Pick the pinned versioned endpoint over the moving family endpoint."""

    urls = item.get("transport_urls")
    if not isinstance(urls, dict):
        raise ConnectorError(f"MCP version {name}@{version} omitted transport_urls")
    url = urls.get("versioned_streamable_http") or urls.get("streamable_http")
    if not isinstance(url, str) or not url:
        raise ConnectorError(f"MCP version {name}@{version} returned no usable endpoint")
    return url


def _url_from_server(server: Any) -> str:
    """Resolve the endpoint of a freshly deployed server from its Admin result."""

    urls = server.transport_urls
    if urls is None:
        raise ConnectorError("deployed MCP server omitted transport URLs")
    url = urls.versioned_streamable_http or urls.streamable_http
    if not url:
        raise ConnectorError("deployed MCP server returned no usable endpoint")
    return url


def _token_env(name: str) -> str:
    """Suggest a collision-resistant token env var name; never the token itself."""

    return "HARNEST_MCP_" + re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") + "_TOKEN"


def _mcp_config(name: str, description: str, services: Sequence[Any]) -> dict[str, Any]:
    """Build a declarative ``kind: mcp`` config from the Studio's structured fields."""

    return {
        "apiVersion": "fused/v1",
        "kind": "mcp",
        "name": name,
        "version": "1.0.0",
        # The Engine requires a non-empty description, so a blank one falls
        # back to the server name rather than failing the deploy.
        "description": description.strip() or name,
        "bucket": "default",
        "services": {
            service.slug: _service_selection(service) for service in services
        },
    }


def _service_selection(service: Any) -> dict[str, Any]:
    """Choose ``select_all`` or an explicit operation allowlist for one service."""

    # An empty allowlist means every operation, so it collapses back to the
    # select_all form rather than producing an unbounded-looking config.
    if service.select_all or not service.operations:
        return {"version": service.version, "select_all": True}
    return {"version": service.version, "operations": list(service.operations)}
