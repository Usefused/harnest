"""Independent local HTTP application for the Harnest Agent Builder."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
import hmac
from pathlib import Path
from importlib.resources import files as resource_files
import secrets
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from harnest.fused_connectors import FusedConnectorsService
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.base import RequestResponseEndpoint
import yaml

from . import workflow, folders, extensions, ownership, deletion, deployment, deployment_overview
from .catalog import catalog, resource_name, template
from .commands import Command, arguments
from .files import Workspace, inventory, read
from .jobs import Jobs
from .evaluations import presets
from .prompting import Prompt, propose, settings
from .assistant_server import AssistantServer
from .features import deployment_enabled, require_deployment
from .mcp_credentials import CredentialStore
from .mcp_service import MCPService
from . import mcp_routes
from .ui_packs import UIPacks
from .packs import Packs
from .types import Completion

STATIC = Path(__file__).parent / "static"
ASSETS = {"host.js", "host-api.js", "host.css"}
SECURITY = {"Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}


class Change(BaseModel):
    """A save must name the exact revision that the user reviewed."""
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(min_length=1, max_length=1024)
    text: str = Field(max_length=1024 * 1024)
    revision: str = Field(max_length=64)


class Save(BaseModel):
    """Apply a bounded source proposal with all revisions checked before writing."""
    model_config = ConfigDict(extra="forbid", strict=True)
    project: str
    files: list[Change] = Field(min_length=1, max_length=24)


class Component(BaseModel):
    """Create a native resource only within the currently selected project."""
    model_config = ConfigDict(extra="forbid", strict=True)
    project: str
    kind: str
    name: str
    options: dict[str, str] = Field(default_factory=dict, max_length=8)


class Wiring(BaseModel):
    """Carry reviewed root source identity with the complete explicit graph topology."""
    model_config = ConfigDict(extra="forbid", strict=True)
    project: str
    revision: str
    edges: list[dict] = Field(max_length=300)


class Conversion(BaseModel):
    """Require the reviewed root revision before wrapping an Agent as a workflow."""
    model_config = ConfigDict(extra="forbid", strict=True)
    project: str
    revision: str


def create_app(root: Path, cli: str | list[str] | tuple[str, ...], *, token: str | None = None,
               completion: Completion | None = None, connector: FusedConnectorsService | None = None,
               credentials: CredentialStore | None = None, packs: Packs | None = None,
               trusted_ui: Sequence[str] = (), safe_ui: bool = False, init_args: Sequence[str] = ()) -> FastAPI:
    """Compose workspace services and the shared host for bundled and custom UI packs."""
    style_nonce = secrets.token_urlsafe(24)
    workspace = Workspace(root)
    workspace.packs = Packs() if safe_ui else packs or Packs()
    ui_packs = UIPacks(workspace.packs, () if safe_ui else trusted_ui)
    credentials = credentials or CredentialStore()
    jobs = Jobs(cli, workspace, credentials, init_args=init_args)
    mcp = MCPService(workspace, jobs, credentials, connector)
    from .builder_config import BuilderConfiguration
    builder = BuilderConfiguration(workspace.packs)
    workspace.builder_environment = builder.environment
    assistant = AssistantServer(environment=builder.environment)
    token = token or secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Terminate supervised CLI processes even when the builder is interrupted."""
        try:
            yield
        finally:
            jobs.close()
            try:
                await assistant.close()
            finally:
                builder.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token, app.state.jobs = token, jobs
    app.state.assistant = assistant
    app.state.mcp = mcp

    @app.middleware("http")
    async def boundary(request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Require loopback, same-origin requests, and a launch-specific bearer capability."""
        try:
            _authorize(request, token)
            if request.url.path.startswith("/api/deployment/"):
                require_deployment()
        except HTTPException as error:
            return JSONResponse({"detail": error.detail}, status_code=error.status_code, headers=SECURITY)
        response = await call_next(request)
        frame_policy = response.headers.get("Content-Security-Policy")
        response.headers.update(SECURITY)
        response.headers["Content-Security-Policy"] = SECURITY["Content-Security-Policy"].replace("style-src 'self'", f"style-src 'self' 'nonce-{style_nonce}'")
        if frame_policy is not None:
            # Panel HTML keeps its opaque sandbox even when opened outside the host page.
            response.headers["Content-Security-Policy"] = frame_policy
        if request.url.path == "/api/workspace" and response.status_code == 200:
            # Cookies survive tab recreation; bind them to this launch and port so
            # another local builder cannot accidentally replace this connection.
            name, value = _session_cookie(request, token)
            response.set_cookie(name, value, httponly=True, samesite="strict", path="/api/", max_age=30 * 24 * 60 * 60)
        return response

    @app.exception_handler(OSError)
    async def filesystem_error(_request: Request, _error: OSError) -> JSONResponse:
        """Report filesystem failures without sending internal paths or environment details."""
        return JSONResponse({"detail": "The filesystem operation failed. Check file permissions and available disk space."}, status_code=500)

    @app.get("/")
    def index() -> Response:
        """Decode the bundled UTF-8 entrypoint independently of the host's locale."""
        return Response((STATIC / "index.html").read_text(encoding="utf-8").replace("__EDITOR_STYLE_NONCE__", style_nonce), media_type="text/html")

    @app.get("/assets/{filename}")
    def asset(filename: str) -> Response:
        """Serve fixed Studio assets and the runtime-owned shared dropdown controls."""
        if filename in {"selects.js", "selects.css"}:
            media = "text/javascript" if filename.endswith(".js") else "text/css"
            return Response(resource_files("harnest").joinpath("_playground", filename).read_bytes(), media_type=media)
        if filename not in ASSETS:
            raise HTTPException(404, "Asset not found.")
        return FileResponse(STATIC / filename)

    @app.get("/api/ui")
    def ui_catalog(safe: bool = False) -> dict[str, Any]:
        """Expose validated composition only after workspace authentication."""
        try:
            return ui_packs.catalog(safe or safe_ui)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/ui-assets/{identity}/{revision}/{relative:path}")
    def ui_asset(identity: str, revision: str, relative: str) -> Response:
        """Serve immutable pack snapshots under their content revision."""
        return ui_packs.asset(identity, revision, relative)

    mcp_routes.install_routes(app, mcp)
    folders.install_routes(app, workspace)
    from . import pack_routes
    pack_routes.install_routes(app, workspace, jobs, workspace.packs)
    extensions.install_routes(app, workspace)
    ownership.install_routes(app, workspace, jobs, _configuration)
    deletion.install_routes(app, workspace, jobs)
    from . import transports
    transports.install_routes(app, workspace)
    deployment.install_routes(app, workspace)
    deployment_overview.install_routes(app, workspace)
    from . import diagnostics
    diagnostics.install_routes(app, workspace)
    _install_reads(app, workspace, jobs)
    _install_edits(app, workspace, jobs)
    _install_commands(app, workspace, jobs)
    from . import preview
    preview.install_routes(app, workspace, jobs)

    @app.post("/api/propose")
    async def proposal(body: Prompt, request: Request) -> JSONResponse:
        """Return provider-backed proposals for explicit review without writing or running them."""
        browser = mcp_routes.session_id(request) or secrets.token_urlsafe(32)
        with mcp_routes.translated():
            result = await propose(workspace, body, completion or assistant.completion, mcp=mcp, session=browser)
        response = JSONResponse(result)
        if result.get("mcp_review") and not mcp_routes.session_id(request):
            response.set_cookie(mcp_routes.cookie_name(request), browser, httponly=True, samesite="lax", path="/", max_age=86400)
        return response

    from .agui import install_routes as install_agui
    install_agui(app, workspace, completion or assistant.completion, mcp)
    return app


def _authorize(request: Request, token: str) -> None:
    """Stop DNS rebinding and cross-origin browsers before they can reach source or processes."""
    local = {"localhost", "127.0.0.1", "::1"}
    if request.client is None or request.client.host not in local or request.url.hostname not in local:
        raise HTTPException(403, "The agent builder is available on loopback only.")
    # Only this callback may arrive cross-site; one-time state, PKCE and the
    # initiating browser cookie replace the normal launch capability here.
    if (request.method, request.url.path) == ("GET", mcp_routes.CALLBACK):
        return
    origin = request.headers.get("origin")
    if origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(403, "Cross-origin requests are forbidden.")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Cross-site requests are forbidden.")
    if request.url.path.startswith("/api/") and not _has_credentials(request, token):
        raise HTTPException(401, "Open the launch URL printed in the builder terminal to connect.")


def _session_cookie(request: Request, token: str) -> tuple[str, str]:
    """Derive a browser-only capability without exposing the launch token to scripts."""
    port = request.url.port or 80
    name = f"harnest-builder-{port}"
    value = hmac.new(token.encode(), f"browser-session:{port}".encode(), "sha256").hexdigest()
    return name, value


def _has_credentials(request: Request, token: str) -> bool:
    """Honor explicit bearer credentials, otherwise resume this launch's browser session."""
    authorization = request.headers.get("authorization")
    if authorization:
        return hmac.compare_digest(authorization.encode(), ("Bearer " + token).encode())
    name, value = _session_cookie(request, token)
    return hmac.compare_digest(request.cookies.get(name, "").encode(), value.encode())


def _install_reads(app: FastAPI, workspace: Workspace, jobs: Jobs) -> None:
    """Expose project metadata and bounded current source snapshots."""

    @app.get("/api/workspace")
    def workspace_info() -> dict[str, Any]:
        """List projects alongside available capabilities and provider configuration."""
        with workspace.lock:
            return {"name": workspace.root.name, "path": str(workspace.root), "projects": workspace.projects(), "catalog": catalog(), "packs": workspace.packs.catalog(), "llm": settings(workspace.builder_environment), "features": {"deployment": deployment_enabled()}, "cli": {"command": list(jobs.command), "init_args": list(jobs.init_args)}}

    @app.get("/api/project")
    def project_info(project: str) -> dict[str, Any]:
        """Keep malformed configuration editable even when it cannot compile yet."""
        root = workspace.project(project)
        with workspace.lock:
            files = inventory(root)
            document = read(root, "agent.py")
            config = _configuration(root)
            connections = ownership.describe(root, files, config)
            try:
                graph = workflow.describe(document["text"])
            except SyntaxError:
                graph = {"available": False, "reason": "Fix the Python syntax in agent.py to enable visual wiring."}
        return {"id": project, "path": str(root), "files": files, "config": config, "ownership": connections, "graph": {**graph, "revision": document["revision"]}}

    @app.get("/api/file")
    def document(project: str, path: str) -> dict[str, Any]:
        """Read exact on-disk text so external editor changes are reflected on refresh."""
        with workspace.lock:
            return read(workspace.project(project), path)

    @app.get("/api/evaluation-metrics")
    def evaluation_metrics() -> dict[str, Any]:
        """Use the installed CLI as the authoritative evaluation authoring catalog."""
        return {"metrics": presets(jobs.command, workspace.root)}

    @app.get("/api/jobs")
    def job_list() -> dict[str, Any]:
        """Expose bounded command history including nonzero exit status."""
        return {"jobs": jobs.list()}


def _configuration(root: Path) -> dict:
    """Extract non-secret project labels; raw environment values stay in the code editor."""
    try:
        config = yaml.safe_load(read(root, "config.yaml")["text"])
        framework = config.get("spec", {}).get("framework", {})
        return {"name": config.get("metadata", {}).get("displayName", root.name), "framework": framework.get("name", "adk"), "mode": framework.get("mode", "managed")}
    except (yaml.YAMLError, AttributeError):
        return {"name": root.name, "framework": "unknown", "mode": "unknown"}


def _install_edits(app: FastAPI, workspace: Workspace, jobs: Jobs) -> None:
    """Serialize user source edits against CLI filesystem operations."""

    @app.put("/api/files")
    def save(body: Save) -> dict[str, Any]:
        """Preserve unseen changes with a transaction-wide revision preflight."""
        with workspace.lock, jobs.lock:
            jobs.ensure_idle()
            return {"files": workspace.apply(body.project, [c.model_dump() for c in body.files])}

    @app.post("/api/component")
    def component(body: Component) -> dict[str, Any]:
        """Create native resources and wire graph agents in the same reviewed transaction."""
        with workspace.lock, jobs.lock:
            jobs.ensure_idle()
            root = workspace.project(body.project)
            sources = template(body.kind, body.name, body.options)
            changes = [{"path": p, "text": t, "revision": ""} for p, t in sources.items()]
            if body.kind == "node":
                current = read(root, "agent.py")
                changes.append({**current, "text": workflow.add_node(current["text"], resource_name(body.name))})
            return {"files": workspace.apply(body.project, changes)}

    @app.put("/api/graph")
    def wiring(body: Wiring) -> dict[str, Any]:
        """Write canvas connections into actual Graph edges while preserving surrounding source."""
        with workspace.lock, jobs.lock:
            jobs.ensure_idle()
            document = read(workspace.project(body.project), "agent.py")
            if document["revision"] != body.revision:
                raise HTTPException(409, "The graph changed on disk. Refresh before reconnecting nodes.")
            text = workflow.connect(document["text"], body.edges)
            return {"files": workspace.apply(body.project, [{**document, "text": text}])}

    @app.post("/api/graph/convert")
    def convert(body: Conversion) -> dict[str, Any]:
        """Keep the original Agent and instructions when creating its first explicit workflow."""
        with workspace.lock, jobs.lock:
            jobs.ensure_idle()
            root = workspace.project(body.project)
            document = read(root, "agent.py")
            if document["revision"] != body.revision:
                raise HTTPException(409, "The root changed on disk. Refresh before converting it.")
            text = workflow.to_graph(document["text"], read(root, "instructions.md")["text"])
            return {"files": workspace.apply(body.project, [{**document, "text": text}])}


def _install_commands(app: FastAPI, workspace: Workspace, jobs: Jobs) -> None:
    """Execute only validated CLI operations and provide explicit cancellation."""

    @app.post("/api/command")
    def command(body: Command) -> dict[str, Any]:
        """Resolve every destination before launching the public Harnest CLI executable."""
        with workspace.lock, jobs.lock:
            args, project = arguments(workspace, body)
            return jobs.start(args, project, serving=body.action == "serve")

    @app.post("/api/jobs/{identity}/stop")
    def stop(identity: str) -> dict[str, Any]:
        """Stop the chosen command without killing unrelated user processes."""
        return jobs.stop(identity)
