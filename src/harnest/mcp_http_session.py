"""A protocol-aware HTTP session projected into framework-compatible result models."""

from contextlib import asynccontextmanager
from functools import partial
from types import SimpleNamespace
from typing import Any

from .mcp_http_transport import InitializedHTTPRequired, ProtocolError, _VERSION, _call, _client
from .mcp_resources import MCPResourceError


@asynccontextmanager
async def modern_session(configured: Any, framework: str):
    """Probe only HTTP; fall back solely on explicit unsupported-method/version errors."""

    if configured.transport != "streamable-http":
        yield None
        return
    async with _client(configured, framework) as (client, url):
        session = HTTPSession(client, url, configured.max_content_bytes)
        try:
            discovered = await session.request("server/discover", {})
        except InitializedHTTPRequired:
            discovered = None
        except ProtocolError as error:
            if error.code not in (-32601, -32022):
                raise
            discovered = None
        if discovered is not None:
            if _VERSION not in discovered.get("supportedVersions", []):
                raise MCPResourceError("MCP discovery does not support protocol 2026-07-28")
            yield session, discovery_info(discovered)
        else:
            yield None


def discovery_info(result: dict[str, Any]) -> Any:
    """Normalize modern identity placement while preserving optional capabilities."""

    from mcp.types import Implementation, ServerCapabilities

    info = result.get("_meta", {}).get("io.modelcontextprotocol/serverInfo", {})
    return SimpleNamespace(
        protocolVersion=_VERSION,
        serverInfo=Implementation.model_validate({"name": "unknown", "version": "unknown", **info}),
        capabilities=ServerCapabilities.model_validate(result.get("capabilities", {})),
    )


class HTTPSession:
    """Use SDK data models, but never the SDK 1.x initialize/session transport."""

    def __init__(self, client: Any, url: str, limit: int) -> None:
        self.client, self.url, self.limit = client, url, limit

    async def request(self, method: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """Send exactly once; uncertain outcomes must never be replayed automatically."""

        return await _call(self.client, self.url, method, params, self.limit, **kwargs)

    def __getattr__(self, name: str) -> Any:
        """Adapt only the finite read operations required by the shared resource facade."""

        from mcp import types

        operations = {
            "list_tools": ("tools/list", types.ListToolsResult),
            "list_resources": ("resources/list", types.ListResourcesResult),
            "list_resource_templates": ("resources/templates/list", types.ListResourceTemplatesResult),
            "list_prompts": ("prompts/list", types.ListPromptsResult),
            "read_resource": ("resources/read", types.ReadResourceResult),
            "get_prompt": ("prompts/get", types.GetPromptResult),
        }
        if name not in operations:
            raise AttributeError(name)
        return partial(self._typed_request, *operations[name])

    async def _typed_request(self, method: str, model: Any, **params: Any) -> Any:
        """Validate remote shapes before returning them to a framework or developer."""

        result = await self.request(method, {key: value for key, value in params.items() if value is not None})
        return model.model_validate(result)
