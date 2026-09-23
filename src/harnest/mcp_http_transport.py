"""Shared bounded 2026 HTTP transport for discovery, tools, and subscriptions."""

from __future__ import annotations

from contextlib import asynccontextmanager
import base64
import json
from typing import Any
from uuid import uuid4

from .mcp_resources import MCPResourceError

_VERSION = "2026-07-28"


class ProtocolError(MCPResourceError):
    """Retain numeric codes for negotiation without exposing provider error text."""

    def __init__(self, code: int) -> None:
        super().__init__("MCP protocol request failed")
        self.code = code


class InitializedHTTPRequired(MCPResourceError):
    """An old HTTP server rejected the stateless discovery probe with HTTP 400."""


def request_headers(method: str, params: dict[str, Any]) -> dict[str, str]:
    """Mirror routing metadata without allowing names or URIs to inject headers."""

    headers = {"Mcp-Method": method}
    key = {"resources/read": "uri", "tools/call": "name", "prompts/get": "name"}.get(method)
    if key is not None:
        headers["Mcp-Name"] = header_value(params[key])
    return headers


def header_value(value: str) -> str:
    """Encode unsafe or sentinel-looking values with the protocol's UTF-8 form."""

    safe = value == value.strip() and all(32 <= ord(char) <= 126 for char in value)
    sentinel = value.startswith("=?base64?") and value.endswith("?=")
    if safe and not sentinel:
        return value
    return "=?base64?" + base64.b64encode(value.encode()).decode() + "?="


def _request(method: str, params: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Carry the stateless protocol's required metadata on every request."""

    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": {
        **params, "_meta": {
            "io.modelcontextprotocol/protocolVersion": _VERSION,
            "io.modelcontextprotocol/clientInfo": {"name": "harnest", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {},
        },
    }}


@asynccontextmanager
async def _client(configured: Any, framework: str):
    """Reuse authored credential/certificate policy; never follow endpoint redirects."""

    import httpx
    from mcp.shared._httpx_utils import create_mcp_http_client

    options = configured.to_langgraph_connection()
    factory = options.get("httpx_client_factory", create_mcp_http_client)
    binding = configured._lifecycle_binding(framework)
    if binding is not None:
        factory = binding.client_factory()
    headers = {**options["headers"], "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": _VERSION}
    timeout = httpx.Timeout(configured.timeout_seconds, read=configured.sse_read_timeout_seconds)
    async with factory(headers=headers, timeout=timeout) as client:
        yield client, options["url"]


async def _messages(response: Any, limit: int):
    """Bound each raw SSE frame before JSON decoding, including comment-only floods."""

    response.raise_for_status()
    pending = b""
    async for chunk in response.aiter_bytes():
        pending += chunk
        pending = pending.replace(b"\r\n", b"\n")
        while b"\n\n" in pending:
            frame, pending = pending.split(b"\n\n", 1)
            if len(frame) > limit:
                raise MCPResourceError("MCP subscription frame exceeds max_content_bytes")
            data = b"\n".join(line[5:].lstrip(b" ") for line in frame.split(b"\n") if line.startswith(b"data:"))
            if data:
                yield json.loads(data)
        if len(pending) > limit:
            raise MCPResourceError("MCP subscription frame exceeds max_content_bytes")


async def _call(client: Any, url: str, method: str, params: dict[str, Any], limit: int, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """Bound total RPC duration on every supported Python version, including 3.11."""

    import anyio

    # Stay in the owning task so stream cleanup runs inside the same cancel scope.
    # asyncio.timeout is unavailable on our minimum supported Python version.
    with anyio.fail_after(client.timeout.connect or 30):
        return await _exchange(client, url, method, params, limit, headers=headers)


async def _exchange(client: Any, url: str, method: str, params: dict[str, Any], limit: int, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """Accept JSON or SSE one-shot results without buffering unbounded responses."""

    request_id = uuid4().hex
    async with client.stream("POST", url, json=_request(method, params, request_id),
                             headers={**(headers or {}), **request_headers(method, params)}, follow_redirects=False) as response:
        # Parse bounded negotiation errors, but never downgrade on auth or 5xx.
        if response.status_code not in (400, 404):
            response.raise_for_status()
        if "text/event-stream" in response.headers.get("content-type", ""):
            async for message in _messages(response, limit):
                if message.get("id") == request_id:
                    return _result(message)
            raise MCPResourceError("MCP response ended without a result")
        contents = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=1024):
            contents.extend(chunk)
            if len(contents) > limit:
                raise MCPResourceError("MCP response exceeds max_content_bytes")
        return _decode_response(contents, response, method, request_id)


def _decode_response(contents: bytes, response: Any, method: str, request_id: str) -> dict[str, Any]:
    """Recognize old HTTP 400 probes without downgrading modern policy failures."""

    try:
        message = json.loads(contents)
    except ValueError:
        if response.status_code == 400 and method == "server/discover":
            raise InitializedHTTPRequired("MCP server requires initialized HTTP") from None
        raise MCPResourceError("MCP response is not valid JSON") from None
    if not isinstance(message, dict):
        raise MCPResourceError("MCP response is not an object")
    code = message.get("error", {}).get("code")
    if response.status_code == 400 and method == "server/discover" and code in (-32600, -32700):
        raise InitializedHTTPRequired("MCP server requires initialized HTTP")
    return _correlated_result(message, response, request_id)


def _correlated_result(message: dict[str, Any], response: Any, request_id: str) -> dict[str, Any]:
    """Require correlation even for errors and never accept HTTP failures as success."""

    if message.get("id") != request_id:
        raise MCPResourceError("MCP response ID mismatch")
    result = _result(message)
    response.raise_for_status()
    return result


def _result(message: dict[str, Any]) -> dict[str, Any]:
    """Do not expose provider error text or mistake input-required responses for data."""

    result = message.get("result")
    if "error" in message:
        raise ProtocolError(message["error"].get("code", 0))
    if not isinstance(result, dict):
        raise MCPResourceError("MCP response has no result object")
    if result.get("resultType", "complete") != "complete":
        raise MCPResourceError("MCP request requires additional input; automatic replay is disabled")
    return result
