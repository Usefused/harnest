"""Adapt portable MCP packages to Harnest without changing their source files."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
from typing import Any, Iterator

from .agent_plugin_manifest import AgentPluginError, contained, diagnostic

_INSTALLATION: ContextVar[str | None] = ContextVar("harnest_plugin_installation", default=None)
_VARIABLES = re.compile(r"\$\{(PLUGIN_ROOT|PLUGIN_DATA)\}")


def installation_id(root: Path) -> str:
    """Keep writable state stable across recompiles but separate across applications."""
    return hashlib.sha256(str(root.resolve()).encode()).hexdigest()


@contextmanager
def plugin_installation(scope: str) -> Iterator[None]:
    """Restore the compiler-owned installation identity in immutable artifacts."""
    if not re.fullmatch(r"[a-f0-9]{64}", scope):
        raise ValueError("invalid Agent Plugin installation identity")
    token = _INSTALLATION.set(scope)
    try:
        yield
    finally:
        _INSTALLATION.reset(token)


@dataclass(frozen=True)
class PortableMCP:
    """Retain immutable package identity and defer state creation until connection."""
    root: Path
    plugin_name: str
    server_name: str
    scope: str

    @classmethod
    def create(cls, root: Path, plugin_name: str, server_name: str) -> "PortableMCP":
        """Use the original application scope when recompiling an artifact source."""
        scope = _INSTALLATION.get() or installation_id(root.parent.parent)
        return cls(root.resolve(), plugin_name, server_name, scope)

    def data_directory(self) -> Path:
        """Use a client-controlled data root, never a plugin-selected host path."""
        default = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "harnest" / "agent-plugins"
        base = Path(os.environ.get("HARNEST_PLUGIN_DATA_DIR", str(default))).expanduser().absolute()
        target = base / self.scope / self.plugin_name
        _no_symlinks(target)
        return target

    def prepare(self) -> None:
        """Create private persistent state only when a server is about to connect."""
        directory = self.data_directory()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _no_symlinks(directory)

    def stdio(self, client: Any) -> dict[str, Any]:
        """Expand only standard variables, once, and confine the configured cwd."""
        data = self.data_directory()
        values = {"PLUGIN_ROOT": str(self.root), "PLUGIN_DATA": str(data)}
        expand = lambda value: _VARIABLES.sub(lambda match: values[match.group(1)], value)
        command = client.command or ""
        if command.startswith("./"):
            command = str(contained(self.root, self.root / command))
        cwd = client.cwd or "./"
        boundary = data if cwd.startswith("${PLUGIN_DATA}") else self.root
        resolved_cwd = contained(boundary, boundary / expand(cwd))
        env = {key: expand(value) for key, value in client.env.items()}
        env.update(values)
        return {"command": command, "args": [expand(arg) for arg in client.args],
                "env": env, "cwd": str(resolved_cwd)}

    def failed(self, error: Exception) -> None:
        """Keep provider exception text out of public diagnostics and model output."""
        diagnostic(self.root, f"MCP server {self.server_name!r} unavailable ({type(error).__name__}); other components remain enabled. Check the server command, connection and authentication.")


def _no_symlinks(path: Path) -> None:
    """Reject symlink-based redirection anywhere in the client-owned state path."""
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise AgentPluginError("Agent Plugin data directory must not contain symlinks")


def portable_http_factory(url: str):
    """Block cross-origin redirects and SSE endpoints before forwarding headers."""
    import httpx
    endpoint = httpx.URL(url)
    origin = (endpoint.scheme, endpoint.host, endpoint.port)

    async def same_origin(request):
        """Never send package headers to a different server without authorization."""
        target = request.url
        if (target.scheme, target.host, target.port) != origin:
            raise AgentPluginError("Agent Plugin MCP request attempted a different origin")

    def create_client(headers=None, timeout=None, auth=None):
        """Preserve MCP authentication while enforcing the package origin boundary."""
        options = {"headers": headers, "auth": auth, "follow_redirects": False,
                   "event_hooks": {"request": [same_origin]}}
        if timeout is not None:
            options["timeout"] = timeout
        return httpx.AsyncClient(**options)

    return create_client


def portable_adk_toolset(base, client):
    """Make unavailable portable servers nonfatal to ADK's independent tools."""
    class PortableToolset(base):
        _portable_disabled = False

        async def get_tools(self, readonly_context=None):
            """Defer writable state until discovery and contain connection failures."""
            if self._portable_disabled:
                return []
            try:
                if client.transport == "stdio":
                    client.portable.prepare()
                return await super().get_tools(readonly_context)
            except Exception as error:
                self._portable_disabled = True
                client.portable.failed(error)
                return []

    return PortableToolset


def disabled_adk_toolset():
    """Preserve native toolset lifecycle when a portable server cannot be configured."""
    from google.adk.tools.base_toolset import BaseToolset

    class DisabledToolset(BaseToolset):
        async def get_tools(self, readonly_context=None):
            """Expose no tools without retrying invalid configuration each turn."""
            return []

        async def close(self):
            """No connection was opened, so there is no provider state to release."""

    return DisabledToolset()
