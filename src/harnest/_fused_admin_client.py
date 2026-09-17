"""Vendored Fused Admin management client (standard library only).

Copied from fused-cli's built-in ``fused-admin`` Python client so the Studio can
list, deploy, and mint tokens for Engine-hosted MCP servers without a fused-cli
install or session. It authenticates with an OAuth access token obtained from
the Fused Auth client.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


class FusedAdminError(Exception):
    """Raised when the Engine returns a GraphQL error or a non-2xx status."""

    def __init__(
        self,
        message: str,
        status: Optional[int] = None,
        errors: Optional[List[dict]] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.errors = errors or []


@dataclass
class FusedAdminConfig:
    """Connection settings for one Fused Engine."""

    engine_url: str
    access_token: str


@dataclass
class MCPServerTransportUrls:
    streamable_http: str
    sse: str
    versioned_streamable_http: str
    versioned_sse: str


@dataclass
class MCPServer:
    """A single MCP server version; `id` is the app id, not the family id."""

    id: str
    name: str
    version: str
    config_key: Optional[str] = None
    default_transport: Optional[str] = None
    transport_urls: Optional[MCPServerTransportUrls] = None
    stable: Optional[bool] = None
    stable_version_id: Optional[str] = None
    execution_token: Optional[str] = None
    active: Optional[bool] = None
    deactivated_at: Optional[str] = None
    created_at: Optional[str] = None


@dataclass
class MCPServerList:
    items: List[MCPServer]
    total: int


@dataclass
class AppTokenBinding:
    service_slug: str
    auth_name: str
    end_user_ref: str
    resource_id: Optional[str] = None


@dataclass
class AppTokenPayload:
    """The fields accepted by POST /workspace/app-tokens."""

    name: str
    allow: Optional[List[str]] = None
    expires_in: Optional[int] = None
    binding_mode: Optional[str] = None
    bindings: Optional[List[AppTokenBinding]] = None


@dataclass
class AppToken:
    """A freshly minted execution token; `token` is returned exactly once."""

    token: str
    name: str
    allow: List[str]
    expires_at: str
    binding_mode: str
    binding_count: int
    created_at: str


# The minimal, stable MCPServer selection shared by list and deploy.
# execution_token is intentionally omitted: it is a credential, and selecting
# it marks the query as a sensitive read, whose audit preflight fails closed
# for delegated OAuth tokens. Tokens are minted via generate_token instead.
_MCP_SERVER_SELECTION = """
  id
  name
  version
  config_key
  default_transport
  transport_urls {
    streamable_http
    sse
    versioned_streamable_http
    versioned_sse
  }
  stable
  stable_version_id
  active
  deactivated_at
  created_at
"""


def _server_from_dict(data: Dict[str, Any]) -> MCPServer:
    """Converts one GraphQL MCPServer object into its typed dataclass."""
    urls = data.get("transport_urls")
    return MCPServer(
        id=data["id"],
        name=data["name"],
        version=data["version"],
        config_key=data.get("config_key"),
        default_transport=data.get("default_transport"),
        transport_urls=MCPServerTransportUrls(**urls) if urls else None,
        stable=data.get("stable"),
        stable_version_id=data.get("stable_version_id"),
        execution_token=data.get("execution_token"),
        active=data.get("active"),
        deactivated_at=data.get("deactivated_at"),
        created_at=data.get("created_at"),
    )


class FusedAdminClient:
    """Management client for a Fused Engine (MCP servers + app tokens)."""

    def __init__(self, config: FusedAdminConfig) -> None:
        # An absolute origin keeps endpoint joins unambiguous.
        if not config.engine_url.startswith(("http://", "https://")):
            raise ValueError("FusedAdminClient engine_url must be an absolute URL")
        if not config.access_token:
            raise ValueError("FusedAdminClient access_token is required")
        self.engine_url = config.engine_url.rstrip("/")
        self.access_token = config.access_token

    def list_servers(self, limit: int = 10, offset: int = 0) -> MCPServerList:
        """Lists the workspace's MCP servers with simple pagination."""
        query = f"""
        query ListMcpServers($limit: Int, $offset: Int) {{
          mcpServers(limit: $limit, offset: $offset) {{
            items {{ {_MCP_SERVER_SELECTION} }}
            total
          }}
        }}
        """
        data = self._graphql(query, {"limit": limit, "offset": offset})
        servers = data["mcpServers"]
        return MCPServerList(
            items=[_server_from_dict(item) for item in servers["items"]],
            total=int(servers["total"]),
        )

    def deploy_server(self, config: Dict[str, Any], owner_team: Optional[str] = None) -> MCPServer:
        """Deploys a new MCP server from a declarative `kind: mcp` config dict."""
        query = f"""
        mutation DeployMcpServer($config: EngineJSON!, $ownerTeam: String) {{
          deployMcpServer(config: $config, owner_team: $ownerTeam) {{ {_MCP_SERVER_SELECTION} }}
        }}
        """
        data = self._graphql(query, {"config": config, "ownerTeam": owner_team})
        return _server_from_dict(data["deployMcpServer"])

    def resolve_mcp_family_reference(self, reference: str) -> str:
        """Resolves an MCP server name (or id) to its app family id."""
        query = """
        query ResolveAppFamilyReference($reference: String!, $kind: String!) {
          appFamilyReference(reference: $reference, kind: $kind) { id kind }
        }
        """
        data = self._graphql(query, {"reference": reference, "kind": "mcp"})
        resolved = data.get("appFamilyReference") or {}
        # A null or empty id is not authority to issue a credential.
        if not resolved.get("id"):
            raise FusedAdminError(f'No MCP server matches reference "{reference}"')
        return resolved["id"]

    def generate_token(self, reference: str, payload: AppTokenPayload) -> AppToken:
        """Mints a named execution token for an MCP server by name or id."""
        family_id = self.resolve_mcp_family_reference(reference)
        query = urllib.parse.urlencode({"app_family_id": family_id})
        body = {
            "name": payload.name,
            "allow": payload.allow,
            "expires_in": payload.expires_in,
            "binding_mode": payload.binding_mode,
            "bindings": [vars(binding) for binding in payload.bindings]
            if payload.bindings
            else None,
        }
        # Drop None fields so the Engine applies its defaults instead of
        # receiving explicit nulls.
        body = {key: value for key, value in body.items() if value is not None}
        data = self._request("POST", f"/workspace/app-tokens?{query}", body)
        return AppToken(
            token=data["token"],
            name=data["name"],
            allow=data.get("allow", []),
            expires_at=data.get("expires_at", ""),
            binding_mode=data.get("binding_mode", ""),
            binding_count=int(data.get("binding_count", 0)),
            created_at=data.get("created_at", ""),
        )

    def _graphql(self, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        """Sends one management GraphQL request and unwraps its `data` payload."""
        envelope = self._request("POST", "/engine/graphql", {"query": query, "variables": variables})
        if envelope.get("errors"):
            message = "; ".join(error.get("message", "") for error in envelope["errors"])
            raise FusedAdminError(message, errors=envelope["errors"])
        if "data" not in envelope:
            raise FusedAdminError("Engine returned no data")
        return envelope["data"]

    def _request(self, method: str, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """Performs one authenticated JSON request and decodes its body."""
        data = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.access_token}",
        }
        request = urllib.request.Request(
            f"{self.engine_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            # Best-effort decode of the Engine error envelope without masking
            # transport failures as authorization errors.
            try:
                payload = json.loads(error.read().decode("utf-8"))
            except (json.JSONDecodeError, ValueError):
                payload = {}
            message = (
                payload.get("message")
                or payload.get("error")
                or f"Request failed ({error.code})"
            )
            raise FusedAdminError(message, status=error.code) from error
