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

import secrets
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request

from ._fused_admin_client import (
    FusedAdminError,
)
from ._fused_auth_client import FusedAuthError
from .fused_connectors import (
    FusedConnectorsService as PlaygroundConnectorsService,
    ConnectorError, ConnectorResult, _token_env as _token_env,
)

_SESSION_COOKIE = "_harnest_fused_session"
_OAUTH_CALLBACK_PATH = "/_harnest/connectors/oauth/callback"
# After a successful OAuth callback the Studio returns to the MCP connections
# view, which is where the connect action originates.
_MCP_VIEW_URL = "/?view=studio&studioView=connections"


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
        session_id = secrets.token_urlsafe(24)
        url = _translated(request, lambda: active.authorize_url(redirect_uri, session_id))
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
