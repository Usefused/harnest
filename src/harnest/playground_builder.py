"""Local builder routes shared by Studio and evaluation authoring."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request

from .playground_authoring import AuthoringStore, authoring_audit, configured_workspace, mcp_source, require_name
from .playground_mcp import _MCPRequest, _require_local, PlaygroundMCPService


class DocumentWrite(BaseModel):
    """Carry the exact source revision reviewed in the browser."""

    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(max_length=1024)
    revision: str = Field(max_length=64)
    text: str = Field(max_length=1024 * 1024)


class ConnectionWrite(BaseModel):
    """Describe a new managed connection without allowing arbitrary destination paths."""

    model_config = ConfigDict(extra="forbid", strict=True)
    scope: str = "."
    name: str = Field(min_length=1, max_length=100)
    transport: Literal["streamable_http", "stdio"] = "streamable_http"
    endpoint: str = Field(min_length=1, max_length=8192)
    arguments: list[str] = Field(default_factory=list, max_length=100)
    tools: list[str] | None = None
    token_env: str = ""


class ConnectionRemove(BaseModel):
    """Require an explicit revision when removing an agent's connection factory."""

    model_config = ConfigDict(extra="forbid", strict=True)
    path: str
    revision: str


class ConnectionQuery(_MCPRequest):
    """Select a compiled connection by its source identity, including nested agents."""

    path: str = Field(max_length=1024)


class FormWrite(BaseModel):
    """Change only supported literal fields or explicit graph topology."""

    model_config = ConfigDict(extra="forbid", strict=True)
    path: str
    revision: str
    line: int = Field(ge=1)
    fields: dict[str, Any] = Field(default_factory=dict, max_length=20)
    workflow: dict[str, Any] | None = None


def install_builder_routes(router: Any, studio: Any) -> None:
    """Capture CLI authoring ownership once; every operation still requires local access."""

    root = configured_workspace()
    store = AuthoringStore(root, studio) if root is not None else None

    def writable(request: Request) -> AuthoringStore:
        """Reject direct/deployed launchers even if a caller can reach their loopback port."""

        _require_local(request)
        if store is None:
            raise HTTPException(403, "Editing requires harnest serve --reload from the agent workspace")
        return store

    @router.get("/_harnest/authoring", include_in_schema=False)
    def catalog(request: Request) -> Any:
        """Tell both editors whether source persistence is available for this runtime."""

        _require_local(request)
        return store.catalog() if store else {"available": False, "scopes": [], "suites": [], "files": []}

    @router.get("/_harnest/authoring/document", include_in_schema=False)
    def document(path: str, request: Request) -> Any:
        """Read current authoring text and its conflict token rather than compiled text."""

        return writable(request).read(path)

    @router.put("/_harnest/authoring/document", include_in_schema=False)
    def save(body: DocumentWrite, request: Request) -> Any:
        """Save a validated document; compilation remains owned by the reload supervisor."""

        with authoring_audit("save"):
            return writable(request).save(body.path, body.text, body.revision)

    @router.get("/_harnest/authoring/form", include_in_schema=False)
    def form(path: str, line: int, request: Request) -> Any:
        """Describe safe form fields against the current source revision."""

        from .playground_forms import form_description
        value = writable(request).read(path)
        return {**value, **form_description(value["text"], line)}

    @router.put("/_harnest/authoring/form", include_in_schema=False)
    def update_form(body: FormWrite, request: Request) -> Any:
        """Patch a reviewed constructor without reformatting the rest of its source file."""

        with authoring_audit("configure"):
            from .playground_forms import patch_form
            owner = writable(request)
            value = owner.read(body.path)
            if value["revision"] != body.revision:
                raise HTTPException(409, "Source changed; reopen the editor before saving")
            try:
                text = patch_form(value["text"], body.line, body.fields, body.workflow)
            except (ValueError, TypeError, SyntaxError) as exc:
                raise HTTPException(422, "Invalid configuration or workflow") from exc
            return owner.save(body.path, text, body.revision)

    @router.post("/_harnest/authoring/mcp", include_in_schema=False)
    def add_connection(body: ConnectionWrite, request: Request) -> Any:
        """Create an agent-owned factory without replacing an existing connection."""

        with authoring_audit("add_connection"):
            owner = writable(request)
            if studio.mode != "managed" or body.scope not in owner.connection_scopes:
                raise HTTPException(422, "Choose a managed scope containing an Agent. Graphs need an Agent node to consume MCP connections")
            name = require_name(body.name)
            path = f"{body.scope}/mcp/{name}.py".removeprefix("./")
            text = mcp_source(name, body.transport, body.endpoint, body.arguments, body.tools, body.token_env)
            return owner.save(path, text, "")

    @router.delete("/_harnest/authoring/mcp", include_in_schema=False)
    def remove_connection(body: ConnectionRemove, request: Request) -> Any:
        """Detach only this factory from its owning agent; remote servers are unaffected."""

        with authoring_audit("remove_connection"):
            writable(request).remove_mcp(body.path, body.revision)
            return {"saved": True}

    @router.post("/_harnest/studio/mcp/query", include_in_schema=False)
    async def query_connection(body: ConnectionQuery, request: Request) -> Any:
        """Discover only a listed compiled factory, with the existing MCP read-only contract."""

        _require_local(request)
        known = {item["path"] for item in studio.projection["blocks"] if item["kind"] == "mcp"}
        if body.path not in known:
            raise HTTPException(404, "Connection not found in this build")
        path = studio.source / body.path
        if path.parent.name != "mcp":
            raise HTTPException(422, "Inline connection discovery is unavailable; use a managed MCP factory")
        source = PlaygroundMCPService(path.parent.parent, studio.framework)
        try:
            return await source.execute(path.stem, _MCPRequest(**body.model_dump(exclude={"path"})))
        except Exception as exc:
            raise HTTPException(422, "MCP discovery failed; check the connection and server logs") from exc
