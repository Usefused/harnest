"""Fused OAuth-backed MCP management for the Studio's MCP connections tab.

This replaces the previous fused-cli shell-outs with the built-in Fused Auth and
Fused Admin clients. A developer authenticates into their Fused workspace with
OAuth (authorization code + PKCE), and the Studio then lists Engine-hosted MCP
servers, deploys new ones from selected workspace services, and mints a
one-time execution token -- all without a fused-cli install or session.

The OAuth access token lives only in server memory, keyed by an HttpOnly session
cookie, and is never exposed to the browser. The one-time MCP execution token is
returned once and shown to the developer, exactly as ``fused-cli mcp token
generate`` would have printed it.
"""

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
from typing import Any, Callable, Sequence

from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request

from ._fused_admin_client import (
    AppTokenPayload,
    FusedAdminClient,
    FusedAdminConfig,
    FusedAdminError,
)
from ._fused_auth_client import FusedAuthClient, FusedAuthConfig, FusedAuthError

_SESSION_COOKIE = "_harnest_fused_session"
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


def _env(name: str) -> str | None:
    """Read a non-empty environment value, treating blank input as absent."""

    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


class PlaygroundConnectorsService:
    """Authenticate into Fused and manage MCP servers through the Admin client."""

    def __init__(
        self,
        *,
        engine_url: str | None = None,
        registration_key: str | None = None,
        scopes: Sequence[str] = _DEFAULT_SCOPES,
    ) -> None:
        # Explicit arguments win; environment supplies a server-side default so
        # a `harnest serve` process can be configured without touching code.
        # The registration key mints an ephemeral public (PKCE) client at connect
        # time, so no client_id or secret is ever configured.
        self.engine_url = (engine_url or _env("HARNEST_FUSED_ENGINE_URL") or "").rstrip("/")
        self.registration_key = registration_key or _env("HARNEST_FUSED_OAUTH_REGISTRATION_KEY") or ""
        scopes_env = _env("HARNEST_FUSED_OAUTH_SCOPES")
        self.scopes = tuple(scopes_env.split(",")) if scopes_env else tuple(scopes)
        # OAuth state is in-memory by design: the playground is a single-process
        # local development surface, so no persistence or cross-process sharing
        # is required and no token material is ever written to disk.
        self._lock = threading.Lock()
        self._flows: dict[str, tuple[str, str]] = {}
        self._sessions: dict[str, _Session] = {}

    def available(self) -> bool:
        """The feature needs a configured Engine and registration key, not fused-cli."""

        return bool(self.engine_url and self.registration_key)

    def connected(self, session_id: str | None) -> bool:
        """True when a live access token is bound to the browser session."""

        session = self._sessions.get(session_id or "")
        return bool(session and session.expires_at > time.time())

    def redirect_uri(self, request: Request) -> str:
        """Resolve the callback URL the Engine redirects back to after consent.

        Derived from the served origin so it needs no configuration; this exact
        URL is what the developer registers on their public OAuth client.
        """

        return f"{request.url.scheme}://{request.url.netloc}{_OAUTH_CALLBACK_PATH}"

    def authorize_url(self, redirect_uri: str) -> str:
        """Register an ephemeral public client, then begin OAuth with a PKCE pair."""

        client_id, _ = self._register_client(redirect_uri, "Harnest Studio")
        client = self._auth_client(redirect_uri, client_id)
        state = secrets.token_urlsafe(24)
        request = client.authorize_url(scopes=list(self.scopes), state=state)
        # The verifier is the proof-of-possession half of PKCE and must never
        # be exposed; only the state travels in the browser URL. The client id is
        # kept for the callback so the same ephemeral client exchanges the code.
        with self._lock:
            self._flows[state] = (client_id, request.code_verifier)
        return request.url

    def complete_authorization(self, code: str, state: str, redirect_uri: str, session_id: str) -> None:
        """Exchange an authorization code and bind the resulting tokens to the session."""

        with self._lock:
            flow = self._flows.pop(state, None)
        # A missing flow means the state is forged, stale, or already used;
        # failing closed prevents a replayed callback from minting a session.
        if flow is None:
            raise ConnectorError("OAuth state is missing or already consumed")
        client_id, verifier = flow
        tokens = self._auth_client(redirect_uri, client_id).exchange_code(code, verifier)
        with self._lock:
            self._sessions[session_id] = _Session(
                access_token=tokens.access_token,
                refresh_token=tokens.refresh_token,
                expires_at=time.time() + max(tokens.expires_in, 1),
            )

    def disconnect(self, session_id: str | None) -> None:
        """Drop the bound session so a fresh consent can be started."""

        self._sessions.pop(session_id or "", None)

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
            raise ConnectorError("Fused session expired; reconnect your workspace")
        return session.access_token

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

    def _pinned_url(self, session_id: str | None, name: str, version: str) -> str:
        """Resolve one exact, active immutable version's endpoint."""

        matches = [
            item for item in self.servers(session_id, limit=_SERVER_LIST_LIMIT, offset=0)["items"]
            if item["name"] == name and item["version"] == version
        ]
        active = [item for item in matches if item.get("active")]
        if not active:
            raise ConnectorError(f"MCP version {name}@{version} is missing, ambiguous, or inactive")
        return _url_from_dict(active[0], name, version)

    def _auth_client(self, redirect_uri: str, client_id: str) -> FusedAuthClient:
        """Construct the public (PKCE) OAuth client for one ephemeral client."""

        return FusedAuthClient(FusedAuthConfig(
            issuer=self.engine_url,
            client_id=client_id,
            redirect_uri=redirect_uri,
        ))

    def _register_client(self, redirect_uri: str, name: str) -> tuple[str, str]:
        """Dynamically register an ephemeral public client via the registration key.

        Returns the one-time (client_id, client_id_expires_at); the client is
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
                "Authorization": f"Bearer {self.registration_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise ConnectorError(f"Fused OAuth client registration failed ({error.code})") from error
        client_id = payload.get("client_id")
        if not client_id:
            raise ConnectorError("Fused OAuth registration returned no client_id")
        return client_id, payload.get("client_id_expires_at", "")

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


def _require_local(request: Request, *, allow_cross_site: bool = False) -> None:
    """Reject remote callers, DNS rebinding, and cross-origin browser requests.

    ``allow_cross_site`` is reserved for the OAuth callback: a consent redirect
    from the Engine is a legitimate top-level cross-site navigation, and the
    OAuth state plus PKCE verifier already bind it to the flow we started.
    """

    from fastapi import HTTPException

    local = {"localhost", "127.0.0.1", "::1"}
    client = request.client
    if client is None or client.host not in local or request.url.hostname not in local:
        raise HTTPException(status_code=403, detail="Connector discovery is local development only")
    if allow_cross_site:
        return
    origin = request.headers.get("origin")
    expected = f"{request.url.scheme}://{request.url.netloc}"
    if origin is not None and origin != expected:
        raise HTTPException(status_code=403, detail="Cross-origin connector access is forbidden")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-site connector access is forbidden")


class _AddRequest(BaseModel):
    """Select an already-deployed server and immutable version by name."""

    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=63)
    version: str = Field(min_length=1, max_length=64)
    token_name: str = Field(default="studio", min_length=1, max_length=64, alias="tokenName")


class _CreateServiceRef(BaseModel):
    """One workspace service bound into a new MCP server, optionally narrowed to specific operations."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    slug: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    select_all: bool = Field(default=True, alias="selectAll")
    operations: list[str] = Field(default_factory=list)


class _CreateRequest(BaseModel):
    """Structured fields for deploying a new Engine-hosted MCP server."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    name: str = Field(min_length=1, max_length=63)
    description: str = Field(default="", max_length=512)
    services: list[_CreateServiceRef] = Field(min_length=1)
    token_name: str = Field(default="studio", min_length=1, max_length=64, alias="tokenName")


class _TokenRequest(BaseModel):
    """Mint a fresh execution token for an existing server by name."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    name: str = Field(min_length=1, max_length=63)
    token_name: str = Field(default="studio", min_length=1, max_length=64, alias="tokenName")


def install_connector_routes(router: Any, service: PlaygroundConnectorsService | None) -> None:
    """Install fused-auth/fused-admin-backed routes, gated on local access and config.

    Request models are module-level, not nested here: with ``from __future__
    import annotations`` active, FastAPI resolves each annotation by name
    through the module's globals, and a class defined only inside this closure
    cannot be found that way -- it silently stops being treated as a request
    body at all.
    """

    from fastapi import HTTPException
    from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

    def _session_id(request: Request) -> str | None:
        """Read the opaque browser-session cookie, which carries no credential."""

        return request.cookies.get(_SESSION_COOKIE)

    def _guarded(request: Request) -> PlaygroundConnectorsService:
        """Share the local-only, feature-configured check across every route."""

        _require_local(request)
        if service is None or not service.available():
            raise HTTPException(status_code=404, detail="Fused connectors are not configured")
        return service

    def _require_session(request: Request) -> str:
        """Admit only requests carrying a live OAuth session."""

        _guarded(request)
        session_id = _session_id(request)
        if session_id is None or not service.connected(session_id):
            raise HTTPException(status_code=409, detail="Connect your Fused workspace first")
        return session_id

    def _translated(request: Request, action: Callable[[], Any]) -> Any:
        """Map domain failures to the same HTTP shape every connector route uses."""

        try:
            return action()
        except (ConnectorError, FusedAuthError, FusedAdminError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @router.get("/_harnest/connectors", include_in_schema=False)
    async def connectors_status(request: Request) -> dict[str, Any]:
        """Let the frontend render connect/list actions for the current state."""

        _require_local(request)
        return {
            "available": service is not None and service.available(),
            "connected": service is not None and service.connected(_session_id(request)),
        }

    @router.post("/_harnest/connectors/oauth/start", include_in_schema=False)
    async def connectors_oauth_start(request: Request) -> Any:
        """Return the Engine consent URL and bind a session cookie for the callback."""

        _require_local(request)
        active = _guarded(request)
        redirect_uri = active.redirect_uri(request)
        url = _translated(request, lambda: active.authorize_url(redirect_uri))
        session_id = secrets.token_urlsafe(24)
        response = JSONResponse({"url": url})
        response.set_cookie(_SESSION_COOKIE, session_id, httponly=True, samesite="lax")
        return response

    @router.get(_OAUTH_CALLBACK_PATH, include_in_schema=False)
    async def connectors_oauth_callback(request: Request) -> Any:
        """Exchange the consent code, bind the token, and return to the Studio."""

        _require_local(request, allow_cross_site=True)
        if service is None or not service.available():
            return HTMLResponse(_oauth_page("Fused connectors are not configured", ok=False))
        error = request.query_params.get("error")
        if error:
            return HTMLResponse(_oauth_page(f"Fused authorization failed: {error}", ok=False))
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        if not code or not state:
            return HTMLResponse(_oauth_page("Missing OAuth code or state", ok=False))
        session_id = _session_id(request) or secrets.token_urlsafe(24)
        redirect_uri = service.redirect_uri(request)
        try:
            service.complete_authorization(code, state, redirect_uri, session_id)
        except (ConnectorError, FusedAuthError) as exc:
            return HTMLResponse(_oauth_page(str(exc), ok=False))
        response = RedirectResponse(_MCP_VIEW_URL, status_code=303)
        response.set_cookie(_SESSION_COOKIE, session_id, httponly=True, samesite="lax")
        return response

    @router.post("/_harnest/connectors/oauth/disconnect", include_in_schema=False)
    async def connectors_oauth_disconnect(request: Request) -> dict[str, Any]:
        """Forget the bound session so the developer can connect a different workspace."""

        _require_local(request)
        if service is not None:
            service.disconnect(_session_id(request))
        return {"connected": False}

    @router.get("/_harnest/connectors/servers", include_in_schema=False)
    async def connectors_servers(request: Request, limit: int = 20, offset: int = 0) -> Any:
        session_id = _require_session(request)
        return _translated(request, lambda: service.servers(session_id, limit=limit, offset=offset))

    @router.get("/_harnest/connectors/servers/{name}/versions", include_in_schema=False)
    async def connectors_versions(
        name: str, request: Request, limit: int = 20, offset: int = 0,
    ) -> Any:
        session_id = _require_session(request)
        return _translated(request, lambda: service.versions(name, session_id, limit=limit, offset=offset))

    @router.post("/_harnest/connectors/add", include_in_schema=False)
    async def connectors_add(body: _AddRequest, request: Request) -> dict[str, Any]:
        session_id = _require_session(request)
        result = _translated(
            request,
            lambda: service.add_existing(
                session_id, name=body.name, version=body.version, token_name=body.token_name,
            ),
        )
        return _result_payload(result)

    @router.get("/_harnest/connectors/services", include_in_schema=False)
    async def connectors_services(request: Request) -> Any:
        """List the workspace services a new MCP server can bind."""

        session_id = _require_session(request)
        return _translated(request, lambda: service.services(session_id))

    @router.get("/_harnest/connectors/services/{service_id}/operations", include_in_schema=False)
    async def connectors_service_operations(service_id: str, request: Request, version: str = "") -> Any:
        """List one service's operations for narrowing a new MCP server's selection."""

        session_id = _require_session(request)
        return _translated(request, lambda: service.operations(session_id, service_id, version))

    @router.post("/_harnest/connectors/create", include_in_schema=False)
    async def connectors_create(body: _CreateRequest, request: Request) -> dict[str, Any]:
        session_id = _require_session(request)
        return _result_payload(_translated(
            request,
            lambda: service.create(
                session_id, name=body.name, description=body.description,
                services=body.services, token_name=body.token_name,
            ),
        ))

    @router.post("/_harnest/connectors/token", include_in_schema=False)
    async def connectors_token(body: _TokenRequest, request: Request) -> dict[str, Any]:
        session_id = _require_session(request)
        return _translated(
            request,
            lambda: service.token(session_id, name=body.name, token_name=body.token_name),
        )


def _result_payload(result: ConnectorResult) -> dict[str, Any]:
    """Shape the response so the UI can show the URL and the one-time token."""

    return {"name": result.name, "url": result.url, "tokenEnv": result.token_env, "token": result.token}


def _oauth_page(message: str, *, ok: bool) -> str:
    """Render a styled outcome card so the OAuth callback never shows bare text.

    Matches the Studio's dark surface and the Engine's branded "connection
    failed" card instead of a minimal unstyled paragraph.
    """

    accent = "#16a34a" if ok else "#dc2626"
    title = "Connected to Fused" if ok else "Fused connection failed"
    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fused workspace connection</title>
<body style="margin:0;min-height:100vh;display:grid;place-items:center;background:#0f1115;color:#e6e8ec;font-family:system-ui,sans-serif">
<main style="width:min(26rem,calc(100vw - 2rem));border:1px solid #2a2e38;border-left:4px solid {accent};border-radius:.9rem;background:#171a21;padding:1.5rem;box-shadow:0 10px 30px rgba(0,0,0,.4)">
  <h1 style="margin:0 0 .5rem;font-size:1.05rem;color:#fff">{_escape_html(title)}</h1>
  <p style="margin:0;font-size:.9rem;color:#c6cad3">{_escape_html(message)}</p>
  <p style="margin:1rem 0 0;font-size:.8rem;color:#7b8190">Returning to the Studio…</p>
</main>
<script>setTimeout(function () {{ window.location.href = "/?view=studio&studioView=connections"; }}, 1500);</script>
</body>"""


def _escape_html(value: str) -> str:
    """Escape user-visible text in the callback page without a template engine."""

    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&#39;")
    )


__all__ = [
    "ConnectorError",
    "ConnectorResult",
    "PlaygroundConnectorsService",
    "install_connector_routes",
]
