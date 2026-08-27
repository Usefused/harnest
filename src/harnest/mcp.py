"""Declarative MCP client connections for the ADK and LangGraph backends."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Mapping, Sequence

_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")

__all__ = ["MCPClient"]


def _expand(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        try:
            return os.environ[name]
        except KeyError as exc:
            raise ValueError(
                f"MCP configuration references unset environment variable {name}"
            ) from exc

    return _ENV_PATTERN.sub(replace, value)


@dataclass(frozen=True, slots=True)
class MCPClient:
    """A client connection to a stdio, SSE, or Streamable HTTP MCP server."""

    transport: str
    command: str | None = None
    args: Sequence[str] = field(default_factory=tuple)
    env: Mapping[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    tool_filter: Sequence[str] | None = None
    tool_name_prefix: str | None = None
    timeout_seconds: float = 30
    sse_read_timeout_seconds: float = 300

    @classmethod
    def stdio(
        cls,
        command: str,
        *args: str,
        env: Mapping[str, str] | None = None,
        tools: Sequence[str] | None = None,
        prefix: str | None = None,
        timeout_seconds: float = 30,
    ) -> "MCPClient":
        return cls(
            "stdio",
            command=command,
            args=args,
            env=env or {},
            tool_filter=tools,
            tool_name_prefix=prefix,
            timeout_seconds=timeout_seconds,
        )

    @classmethod
    def streamable_http(
        cls,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        tools: Sequence[str] | None = None,
        prefix: str | None = None,
        timeout_seconds: float = 30,
        sse_read_timeout_seconds: float = 300,
    ) -> "MCPClient":
        return cls(
            "streamable-http",
            url=url,
            headers=headers or {},
            tool_filter=tools,
            tool_name_prefix=prefix,
            timeout_seconds=timeout_seconds,
            sse_read_timeout_seconds=sse_read_timeout_seconds,
        )

    @classmethod
    def sse(
        cls,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        tools: Sequence[str] | None = None,
        prefix: str | None = None,
        timeout_seconds: float = 30,
        sse_read_timeout_seconds: float = 300,
    ) -> "MCPClient":
        return cls(
            "sse",
            url=url,
            headers=headers or {},
            tool_filter=tools,
            tool_name_prefix=prefix,
            timeout_seconds=timeout_seconds,
            sse_read_timeout_seconds=sse_read_timeout_seconds,
        )

    def __post_init__(self) -> None:
        self._validate_timeouts()
        self._validate_transport()

    def _validate_timeouts(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("MCP timeout_seconds must be greater than zero")
        if self.sse_read_timeout_seconds <= 0:
            raise ValueError(
                "MCP sse_read_timeout_seconds must be greater than zero"
            )
        if isinstance(self.args, (str, bytes)):
            raise TypeError("MCP args must be a sequence of arguments, not a string")
        if isinstance(self.tool_filter, (str, bytes)):
            raise TypeError("MCP tools must be a sequence of tool names, not a string")

    def _validate_transport(self) -> None:
        if self.transport == "stdio" and (not self.command or not self.command.strip()):
            raise ValueError("stdio MCP clients require a server command")
        if self.transport in {"sse", "streamable-http"} and (
            not self.url or not self.url.strip()
        ):
            raise ValueError(f"{self.transport} MCP clients require a server URL")
        if self.transport not in {"stdio", "sse", "streamable-http"}:
            raise ValueError(f"unsupported MCP transport: {self.transport}")

    def to_adk_toolset(self):
        """Construct an ADK MCP toolset, resolving ``${ENV_VAR}`` placeholders."""

        classes = _adk_mcp_classes()
        connection = self._adk_connection(classes)
        return classes[0](
            connection_params=connection,
            tool_filter=list(self.tool_filter) if self.tool_filter is not None else None,
            tool_name_prefix=self.tool_name_prefix,
        )

    def _adk_connection(self, classes: tuple[Any, ...]) -> Any:
        _, sse_params, stdio_params, stream_params, server_parameters = classes
        if self.transport == "stdio":
            return self._adk_stdio(stdio_params, server_parameters)
        params = sse_params if self.transport == "sse" else stream_params
        return params(
            url=_expand(self.url or ""),
            headers={key: _expand(value) for key, value in self.headers.items()},
            timeout=self.timeout_seconds,
            sse_read_timeout=self.sse_read_timeout_seconds,
        )

    def _adk_stdio(self, params: Any, server_parameters: Any) -> Any:
        server = server_parameters(
            command=_expand(self.command or ""),
            args=[_expand(arg) for arg in self.args],
            env={key: _expand(value) for key, value in self.env.items()} or None,
        )
        return params(server_params=server, timeout=self.timeout_seconds)

    def to_langgraph_connection(self) -> dict[str, Any]:
        """Return a LangChain MCP adapter connection without opening it."""

        if self.transport == "stdio":
            return {
                "transport": "stdio",
                "command": _expand(self.command or ""),
                "args": [_expand(arg) for arg in self.args],
                "env": {key: _expand(value) for key, value in self.env.items()},
                "session_kwargs": {
                    "read_timeout_seconds": timedelta(seconds=self.timeout_seconds)
                },
            }
        return {
            "transport": (
                "streamable_http"
                if self.transport == "streamable-http"
                else self.transport
            ),
            "url": _expand(self.url or ""),
            "headers": {key: _expand(value) for key, value in self.headers.items()},
            "timeout": self.timeout_seconds,
            "sse_read_timeout": self.sse_read_timeout_seconds,
            "session_kwargs": {
                "read_timeout_seconds": timedelta(seconds=self.timeout_seconds)
            },
        }


def _adk_mcp_classes() -> tuple[Any, ...]:
    try:
        from google.adk.tools.mcp_tool import (
            McpToolset, SseConnectionParams, StdioConnectionParams,
            StreamableHTTPConnectionParams,
        )
        from mcp import StdioServerParameters
    except ImportError:  # pragma: no cover - depends on optional runtime
        return _fallback_adk_mcp_classes()
    return (McpToolset, SseConnectionParams, StdioConnectionParams,
            StreamableHTTPConnectionParams, StdioServerParameters)


def _fallback_adk_mcp_classes() -> tuple[Any, ...]:
    # ADK releases differ in what mcp_tool re-exports, so use the defining
    # modules as a compatibility seam for the supported 2.x line.
    try:
        from google.adk.tools.mcp_tool.mcp_session_manager import (
            SseConnectionParams, StdioConnectionParams,
            StreamableHTTPConnectionParams,
        )
        from mcp import StdioServerParameters
        try:
            from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
        except ImportError:
            from google.adk.tools.mcp_tool import McpToolset
    except ImportError as exc:  # pragma: no cover - optional runtime
        raise RuntimeError(
            "google-adk with MCP support is required to use MCPClient"
        ) from exc
    return (McpToolset, SseConnectionParams, StdioConnectionParams,
            StreamableHTTPConnectionParams, StdioServerParameters)
