"""Filesystem contract for standard Agent Plugins.

A portable plugin below ``plugins/`` declares ``plugin.json`` with optional
``mcp.json`` servers and ``skills/`` content. Harnest never infers a plugin
package from its directory contents or imports package Python.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .authoring_errors import folder_entry_error


_IGNORED_NAMES = {"__pycache__", ".DS_Store"}


class PluginConventionError(ValueError):
    """Raised when an installed Agent Plugin violates its package boundary."""


@dataclass(frozen=True, slots=True)
class PluginResources:
    """Resources discovered from one ``plugins/<name>/`` directory.

    ``mcp_clients`` contains standard declarative portable servers and
    ``skill_directories`` contains validated standard skill folders. Paths are
    absolute when the input directory is absolute and otherwise retain the
    caller's path form.
    """

    name: str
    directory: Path
    skill_directories: tuple[Path, ...] = ()
    mcp_clients: tuple[Any, ...] = ()
    manifest: Mapping[str, Any] | None = None


def discover_plugins(directory: str | Path) -> tuple[PluginResources, ...]:
    """Discover only standard Agent Plugin packages deterministically.

    The accepted layout is::

        plugins/
          bigquery-memory/
            plugin.json
            mcp.json
            skills/
              conversation-storage/SKILL.md

    Standard MCP and skill components are optional and validated independently.
    Discovery never imports package code or infers a package without plugin.json.
    """

    plugins_directory = Path(directory)
    _require_optional_directory(plugins_directory, kind="plugins")
    if not plugins_directory.exists():
        return ()

    discovered: list[PluginResources] = []
    for plugin_directory in sorted(
        plugins_directory.iterdir(), key=lambda item: item.name
    ):
        plugin = _discover_agent_plugin(plugin_directory)
        if plugin is not None:
            discovered.append(plugin)
    names = [plugin.name for plugin in discovered]
    if len(names) != len(set(names)):
        raise PluginConventionError("duplicate Agent Plugin manifest name; give each installed plugin a unique name")
    return tuple(discovered)


def _discover_agent_plugin(directory: Path) -> PluginResources | None:
    """Require the standard manifest and never import package Python."""

    if directory.is_symlink():
        raise PluginConventionError(
            f"plugin directory cannot be a symlink: {directory}"
        )
    if _is_ignored(directory):
        return None
    if not directory.is_dir():
        raise PluginConventionError(
            folder_entry_error(f"unexpected resource in plugins directory: {directory}", directory, kind="plugins")
        )
    manifest = directory / "plugin.json"
    if manifest.exists() or manifest.is_symlink():
        from .agent_plugin_loader import discover_portable_plugin
        return discover_portable_plugin(directory)
    if (directory / "plugin.yaml").exists() or (directory / "plugin.yaml").is_symlink():
        raise PluginConventionError(
            f"{directory}: plugin.yaml is retired; run 'harnest upgrade' to "
            "move the executable package into extensions/"
        )
    raise PluginConventionError(
        f"Agent Plugin {directory} must contain plugin.json; Harnest no longer "
        "loads manifestless plugins or Python MCP factories from plugins/"
    )


def _require_optional_directory(path: Path, *, kind: str) -> None:
    if path.is_symlink():
        raise PluginConventionError(f"{kind} directory cannot be a symlink: {path}")
    if path.exists() and not path.is_dir():
        raise PluginConventionError(f"{kind} path must be a directory: {path}")


def _is_ignored(path: Path) -> bool:
    return (
        path.name in _IGNORED_NAMES
        or path.name.startswith(".")
        or path.name.startswith("_")
    )


__all__ = ["PluginConventionError", "PluginResources", "discover_plugins"]
