"""Local authenticated connector routes; OAuth callbacks use their own one-time browser proof."""

from contextlib import contextmanager
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from harnest.fused_connectors import FusedConnectorsService, ConnectorError
from harnest._fused_admin_client import FusedAdminError
from harnest._fused_auth_client import FusedAuthError

from .mcp_plans import MCPPlan, endpoint

CALLBACK = "/fused/oauth/callback"


def cookie_name(request):
    """Isolate OAuth sessions across different loopback Studio launch ports."""
    return "harnest-studio-fused-" + str(request.url.port or 80)


def session_id(request):
    """Keep browser identity out of model payloads and explicit request bodies."""
    return request.cookies.get(cookie_name(request), "")


@contextmanager
def translated():
    """Never expose raw upstream errors, which can echo submitted secrets or headers."""
    try:
        yield
    except (ConnectorError, FusedAdminError, FusedAuthError, OSError):
        raise HTTPException(502, "Fused could not complete this operation. Check the Engine address, workspace access, and connection status.") from None


class Connect(BaseModel):
    """Configure the Engine through an explicit browser action, never through model output."""
    model_config = ConfigDict(extra="forbid", strict=True)
    engine_url: str = Field(min_length=1, max_length=2048)


class Apply(BaseModel):
    """Execute only an opaque review already bound to this browser and project."""
    model_config = ConfigDict(extra="forbid", strict=True)
    review: str = Field(min_length=1, max_length=100)


def install_routes(app, service):
    """Expose UI and AI planning through the same domain service and approval boundary."""

    @app.get("/api/mcp/status")
    def status(request: Request):
        """Return non-secret connection status without sending credentials to the browser."""
        return {"engine_url": service.connector.engine_url, "connected": service.connector.connected(session_id(request))}

    @app.post("/api/mcp/connect")
    def connect(request: Request, body: Connect):
        """Start a browser-bound OAuth flow after the user chooses an Engine."""
        url = endpoint(body.engine_url).rstrip("/")
        parsed = urlsplit(url)
        if parsed.path or (parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}):
            raise HTTPException(422, "Use an HTTPS Engine origin, or HTTP on loopback for development.")
        with service.lock, translated():
            if service.connector.engine_url != url:
                service.connector = FusedConnectorsService(engine_url=url)
                service.reviews.clear()
            browser = secrets.token_urlsafe(32)
            callback = str(request.base_url).rstrip("/") + CALLBACK
            authorize = service.connector.authorize_url(callback, browser)
        response = JSONResponse({"url": authorize})
        response.set_cookie(cookie_name(request), browser, httponly=True, samesite="lax", path="/", max_age=86400)
        return response

    @app.get(CALLBACK)
    def callback(request: Request):
        """Consume state only for the browser that initiated it; return no OAuth payloads."""
        code, state, browser = request.query_params.get("code"), request.query_params.get("state"), session_id(request)
        if not code or not state or not browser or request.query_params.get("error"):
            return HTMLResponse("<h1>Fused connection was not completed</h1><p>Return to Studio and connect again.</p>", status_code=400)
        with translated():
            service.connector.complete_authorization(code, state, str(request.base_url).rstrip("/") + CALLBACK, browser)
        return HTMLResponse('<h1>Connected to Fused</h1><p>You can close this tab and return to Studio.</p><a href="/">Open Studio</a>')

    @app.post("/api/mcp/disconnect")
    def disconnect(request: Request):
        """Forget management access and pending reviews; existing runtime tokens remain separate."""
        browser = session_id(request)
        with service.lock:
            service.connector.disconnect(browser)
            service.reviews = {key: item for key, item in service.reviews.items() if item.session != browser}
        return {"connected": False}

    @app.get("/api/mcp/discover")
    def discover(request: Request, action: str, service_id: str = "", version: str = "", offset: int = 0):
        """Serve bounded discovery for native selectors and operation checkboxes."""
        with translated():
            return service.discover(session_id(request), {"action": action, "service_id": service_id, "version": version, "offset": offset})

    @app.post("/api/mcp/plan")
    def plan(request: Request, body: MCPPlan):
        """Prepare source diffs and a non-mutating provisioning review."""
        browser = session_id(request) or secrets.token_urlsafe(32)
        with translated():
            result = service.prepare(browser, body)
        response = JSONResponse(result)
        if not session_id(request):
            response.set_cookie(cookie_name(request), browser, httponly=True, samesite="lax", path="/", max_age=86400)
        return response

    @app.post("/api/mcp/apply")
    def apply(request: Request, body: Apply):
        """Explicit approval is the only endpoint that creates servers, tokens, or source files."""
        with translated():
            return service.apply(session_id(request), body.review)
