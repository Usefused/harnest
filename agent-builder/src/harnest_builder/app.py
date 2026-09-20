"""Independent local HTTP application for the Harnest Agent Builder."""

from __future__ import annotations

from contextlib import asynccontextmanager
import hmac
from pathlib import Path
import secrets

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
import yaml

from . import workflow, folders, extensions, ownership, deletion, deployment, deployment_overview
from .catalog import catalog, resource_name, template
from .commands import Command, arguments
from .files import Workspace, inventory, read
from .jobs import Jobs
from .prompting import Prompt, propose, settings
from .assistant_server import AssistantServer

STATIC = Path(__file__).parent / "static"
ASSETS = {"index.html", "app.js", "canvas.js", "ui.js", "style.css", "deployment.js"}
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


def create_app(root: Path, cli: str, *, token: str | None = None, completion=None) -> FastAPI:
    """Compose a standalone app with no dependency on Harnest's existing browser UIs."""
    workspace = Workspace(root)
    jobs = Jobs(cli, workspace)
    assistant = AssistantServer()
    token = token or secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(_app):
        """Terminate supervised CLI processes even when the builder is interrupted."""
        try:
            yield
        finally:
            jobs.close()
            await assistant.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token, app.state.jobs = token, jobs
    app.state.assistant = assistant

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        """Require loopback, same-origin requests, and a launch-specific bearer capability."""
        try:
            _authorize(request, token)
        except HTTPException as error:
            return JSONResponse({"detail": error.detail}, status_code=error.status_code, headers=SECURITY)
        response = await call_next(request)
        response.headers.update(SECURITY)
        if request.url.path == "/api/workspace" and response.status_code == 200:
            # Cookies survive tab recreation; bind them to this launch and port so
            # another local builder cannot accidentally replace this connection.
            name, value = _session_cookie(request, token)
            response.set_cookie(name, value, httponly=True, samesite="strict", path="/api/", max_age=30 * 24 * 60 * 60)
        return response

    @app.exception_handler(OSError)
    async def filesystem_error(_request, _error):
        """Report filesystem failures without sending internal paths or environment details."""
        return JSONResponse({"detail": "The filesystem operation failed. Check file permissions and available disk space."}, status_code=500)

    @app.get("/")
    def index():
        """Serve the independent builder entrypoint."""
        return FileResponse(STATIC / "index.html")

    @app.get("/assets/{filename}")
    def asset(filename: str):
        """Serve a fixed asset set, never user-selected filesystem paths."""
        if filename not in ASSETS:
            raise HTTPException(404, "Asset not found.")
        return FileResponse(STATIC / filename)

    folders.install_routes(app, workspace)
    extensions.install_routes(app, workspace)
    ownership.install_routes(app, workspace, jobs, _configuration)
    deletion.install_routes(app, workspace, jobs)
    deployment.install_routes(app, workspace)
    deployment_overview.install_routes(app, workspace)
    _install_reads(app, workspace, jobs)
    _install_edits(app, workspace, jobs)
    _install_commands(app, workspace, jobs)

    @app.post("/api/propose")
    async def proposal(body: Prompt):
        """Return provider-backed proposals for explicit review without writing or running them."""
        return await propose(workspace, body, completion or assistant.completion)

    return app


def _authorize(request: Request, token: str) -> None:
    """Stop DNS rebinding and cross-origin browsers before they can reach source or processes."""
    local = {"localhost", "127.0.0.1", "::1"}
    if request.client is None or request.client.host not in local or request.url.hostname not in local:
        raise HTTPException(403, "The agent builder is available on loopback only.")
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


def _install_reads(app, workspace, jobs) -> None:
    """Expose project metadata and bounded current source snapshots."""

    @app.get("/api/workspace")
    def workspace_info():
        """List projects alongside available capabilities and provider configuration."""
        with workspace.lock:
            return {"name": workspace.root.name, "path": str(workspace.root), "projects": workspace.projects(), "catalog": catalog(), "llm": settings()}

    @app.get("/api/project")
    def project_info(project: str):
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
    def document(project: str, path: str):
        """Read exact on-disk text so external editor changes are reflected on refresh."""
        with workspace.lock:
            return read(workspace.project(project), path)

    @app.get("/api/jobs")
    def job_list():
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


def _install_edits(app, workspace, jobs) -> None:
    """Serialize user source edits against CLI filesystem operations."""

    @app.put("/api/files")
    def save(body: Save):
        """Preserve unseen changes with a transaction-wide revision preflight."""
        with workspace.lock, jobs.lock:
            jobs.ensure_idle()
            return {"files": workspace.apply(body.project, [c.model_dump() for c in body.files])}

    @app.post("/api/component")
    def component(body: Component):
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
    def wiring(body: Wiring):
        """Write canvas connections into actual Graph edges while preserving surrounding source."""
        with workspace.lock, jobs.lock:
            jobs.ensure_idle()
            document = read(workspace.project(body.project), "agent.py")
            if document["revision"] != body.revision:
                raise HTTPException(409, "The graph changed on disk. Refresh before reconnecting nodes.")
            text = workflow.connect(document["text"], body.edges)
            return {"files": workspace.apply(body.project, [{**document, "text": text}])}

    @app.post("/api/graph/convert")
    def convert(body: Conversion):
        """Keep the original Agent and instructions when creating its first explicit workflow."""
        with workspace.lock, jobs.lock:
            jobs.ensure_idle()
            root = workspace.project(body.project)
            document = read(root, "agent.py")
            if document["revision"] != body.revision:
                raise HTTPException(409, "The root changed on disk. Refresh before converting it.")
            text = workflow.to_graph(document["text"], read(root, "instructions.md")["text"])
            return {"files": workspace.apply(body.project, [{**document, "text": text}])}


def _install_commands(app, workspace, jobs) -> None:
    """Execute only validated CLI operations and provide explicit cancellation."""

    @app.post("/api/command")
    def command(body: Command):
        """Resolve every destination before launching the public Harnest CLI executable."""
        with workspace.lock, jobs.lock:
            args, project = arguments(workspace, body)
            return jobs.start(args, project, serving=body.action == "serve")

    @app.post("/api/jobs/{identity}/stop")
    def stop(identity: str):
        """Stop the chosen command without killing unrelated user processes."""
        return jobs.stop(identity)
