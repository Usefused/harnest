"""Emit standard Harnest client factories with environment credential references."""

from dataclasses import fields
from collections.abc import Mapping
import pprint
from typing import Any

from harnest.mcp import MCPClient


def _literal(value: Any) -> Any:
    """Reject executable hooks instead of silently dropping authored behavior."""

    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {_literal(key): _literal(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return tuple(_literal(item) for item in value)
    raise ValueError("write_client cannot serialize lifecycle, approval, or other Python objects; use result.client directly")


def client_source(client: MCPClient) -> str:
    """Write only ordinary MCP fields; Fused provisioning stays outside runtime."""

    defaults = MCPClient.streamable_http(client.url)
    configuration = {item.name: _literal(getattr(client, item.name))
                     for item in fields(MCPClient)
                     if item.init and (item.name in {"transport", "url"}
                                       or getattr(client, item.name) != getattr(defaults, item.name))}
    rendered = pprint.pformat(configuration, sort_dicts=True, width=88)
    return ('"""Connect to the provisioned Fused MCP server."""\n\n'
            'from harnest.mcp import MCPClient\n\n\n'
            'def client() -> MCPClient:\n'
            '    """Resolve the execution token from the deployment environment."""\n'
            f'    return MCPClient(**{rendered})\n')
