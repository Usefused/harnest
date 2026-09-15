"""Local-only developer MCP inspection; never an end-user credential proxy."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request


class _MCPRequest(BaseModel):
    """Bound browser-selected operations to read-only MCP capabilities."""

    model_config = ConfigDict(extra="forbid", strict=True)
    operation: Literal["inspect", "read_resource", "get_prompt", "list_tools", "list_resources", "list_resource_templates", "list_prompts"]
    identifier: str = Field(default="", max_length=8192)
    arguments: dict[str, str] = Field(default_factory=dict, max_length=100)
    cursor: str | None = Field(default=None, max_length=8192)


class PlaygroundMCPService:
    """Inspect direct authored clients without compiling an agent or starting listeners."""

    def __init__(self, source: Path, framework: str) -> None:
        self.source = source.resolve()
        self.framework = framework

    def clients(self) -> list[str]:
        """List filenames without importing factories or exposing endpoint credentials."""

        from .bundle import _resource_files

        return [path.stem for path in _resource_files(self.source / "mcp", kind="mcp")]

    async def execute(self, name: str, request: _MCPRequest) -> dict[str, Any]:
        """Keep the authored namespace bound until the connection has been closed."""

        from ._library import authored_library
        from .bundle import _discover_mcp

        if name not in self.clients():
            raise KeyError(name)
        with authored_library(self.source):
            configured = next(item for item in _discover_mcp(self.source / "mcp") if item.identity == name)
            async with configured.connect(framework=self.framework) as client:
                if request.operation == "inspect":
                    return await client.inspect()
                if request.operation == "read_resource":
                    return await client.read_resource(request.identifier)
                if request.operation == "get_prompt":
                    return await client.get_prompt(request.identifier, request.arguments)
                return await getattr(client, request.operation)(cursor=request.cursor)


def _require_local(request: Request) -> None:
    """Reject remote callers, DNS rebinding, and cross-origin browser requests."""

    from fastapi import HTTPException

    local = {"localhost", "127.0.0.1", "::1"}
    client = request.client
    if client is None or client.host not in local or request.url.hostname not in local:
        raise HTTPException(status_code=403, detail="MCP inspection is local development only; use harnest mcp from the agent folder")
    origin = request.headers.get("origin")
    expected = f"{request.url.scheme}://{request.url.netloc}"
    if origin is not None and origin != expected:
        raise HTTPException(status_code=403, detail="Cross-origin MCP inspection is forbidden")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-site MCP inspection is forbidden")


def install_mcp_routes(router: Any, service: PlaygroundMCPService) -> None:
    """Offer bounded developer reads, never tool invocation or subscription creation."""

    from fastapi import HTTPException

    @router.get("/_harnest/mcp", include_in_schema=False)
    async def clients(request: Request) -> dict[str, Any]:
        """Require local developer access before enumerating connection identities."""

        _require_local(request)
        return {"clients": service.clients()}

    @router.post("/_harnest/mcp/{name}", include_in_schema=False)
    async def execute(name: str, body: _MCPRequest, request: Request) -> dict[str, Any]:
        """Sanitize authoring and provider failures before returning them to a browser."""

        _require_local(request)
        try:
            return await service.execute(name, body)
        except KeyError:
            raise HTTPException(status_code=404, detail="MCP client not found") from None
        except Exception:
            raise HTTPException(status_code=502, detail="MCP inspection failed; check the client configuration and allowed identifiers") from None
