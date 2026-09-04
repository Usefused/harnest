"""Discover standard packages without importing Harnest-specific Python files."""

import hashlib
from pathlib import Path
import re
from types import MappingProxyType

from .agent_plugin_manifest import (
    AgentPluginError, contained, diagnostic, load_manifest, load_servers,
)
from .agent_plugin_runtime import PortableMCP
from .mcp import MCPClient


def discover_portable_plugin(root: Path):
    """Reject invalid manifests before inspecting any executable components."""
    from .plugin import PluginResources
    try:
        manifest = load_manifest(root)
    except (AgentPluginError, OSError) as error:
        detail = str(error) if isinstance(error, AgentPluginError) else "cannot read plugin.json"
        diagnostic(root, f"plugin disabled: {detail}")
        return None
    name = manifest["name"]
    clients = tuple(client for server, value in load_servers(root)
                    if (client := _safe_client(root, name, server, value)) is not None)
    return PluginResources(name, root, skill_directories=_skills(root),
                           mcp_clients=clients, manifest=MappingProxyType(manifest))


def _skills(root: Path) -> tuple[Path, ...]:
    """Validate each immediate skill independently; never import unsupported content."""
    from .skills import SkillValidationError, _filesystem_skill
    directory = root / "skills"
    if not directory.exists() and not directory.is_symlink():
        return ()
    try:
        contained(root, directory)
        if not directory.is_dir():
            raise AgentPluginError("skills must be a directory")
        paths = sorted(directory.iterdir())
    except (AgentPluginError, OSError):
        diagnostic(root, "skills disabled: expected a directory inside the plugin folder")
        return ()
    result = []
    for path in paths:
        if not (path / "SKILL.md").exists() and not (path / "SKILL.md").is_symlink():
            continue
        try:
            contained(root, path / "SKILL.md")
            _filesystem_skill(path)
        except (AgentPluginError, SkillValidationError, OSError, ValueError, RecursionError):
            diagnostic(root, f"skill {path.name!r} skipped: check its SKILL.md metadata and keep resources inside the plugin")
            continue
        result.append(path)
    return tuple(result)


def _safe_client(root: Path, plugin: str, server: str, value: dict) -> MCPClient | None:
    """Keep runtime adapter validation failures isolated to the declared server."""
    try:
        return _client(root, plugin, server, value)
    except (AgentPluginError, OSError, ValueError, TypeError):
        diagnostic(root, f"MCP server {server!r} skipped: configuration could not be mapped to the runtime")
        return None


def _client(root: Path, plugin: str, server: str, value: dict) -> MCPClient:
    """Retain literal configuration; framework adapters expand it only at runtime."""
    portable = PortableMCP.create(root, plugin, server)
    prefix = _identity(plugin, server)
    return MCPClient(
        transport=value["type"], command=value.get("command"),
        args=tuple(value.get("args", ())), env=MappingProxyType(value.get("env", {})),
        cwd=value.get("cwd"), url=value.get("url"),
        headers=MappingProxyType(value.get("headers", {})),
        identity=prefix, capability_id=prefix,
        tool_name_prefix=prefix, portable=portable,
    )


def _identity(plugin: str, server: str) -> str:
    """Keep provider tool prefixes short while avoiding normalization collisions."""
    identity = f"{plugin}/{server}"
    slug = re.sub(r"[^a-zA-Z0-9_]", "_", identity)[:20]
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:12]
    return f"plugin_{slug}_{suffix}"
