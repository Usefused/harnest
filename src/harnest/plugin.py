"""Filesystem contract for reusable agent plugins.

A plugin is a directory below ``plugins/`` that combines MCP client modules
with skills explaining how an agent should use those MCP capabilities.  The
directory is the plugin declaration: authors do not create a Python plugin
object or aggregation module.

This module only discovers and validates filesystem resources.  Importing MCP
modules and validating their exports belongs to the bundle compiler.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .authoring_errors import authoring_guidance, folder_entry_error, inactive_entry_hint


_PLUGIN_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?$")
_IGNORED_NAMES = {"__pycache__", ".DS_Store"}


class PluginConventionError(ValueError):
    """Raised when an authored capability plugin violates its layout contract."""


@dataclass(frozen=True, slots=True)
class PluginResources:
    """Resources discovered from one ``plugins/<name>/`` directory.

    ``mcp_sources`` contains public Python modules from the plugin's ``mcp/``
    directory. ``skill_directories`` contains directories with an uppercase
    ``SKILL.md`` manifest. Paths are absolute when the input plugins directory
    is absolute and otherwise retain the caller's path form.
    """

    name: str
    directory: Path
    mcp_sources: tuple[Path, ...] = ()
    skill_directories: tuple[Path, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.mcp_sources and not self.skill_directories


def discover_plugins(directory: str | Path) -> tuple[PluginResources, ...]:
    """Discover non-empty capability plugins in deterministic name order.

    The accepted layout is::

        plugins/
          bigquery-memory/
            mcp/
              bigquery.py
            skills/
              conversation-storage/SKILL.md

    Empty plugin directories are skipped. A non-empty plugin must have at least
    one MCP module and one skill directory. Public entries outside ``mcp/`` and
    ``skills/`` are rejected. Discovery never imports an MCP module; the bundle
    compiler owns export loading and validation.
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
    return tuple(discovered)


def _discover_agent_plugin(directory: Path) -> PluginResources | None:
    """Validate and classify one manifest-less MCP-plus-skills plugin."""

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
    if not _PLUGIN_NAME.fullmatch(directory.name):
        raise PluginConventionError(
            "plugin directory name may contain only letters, numbers, "
            f"underscores, and hyphens: {directory}"
        )
    if _is_runtime_plugin(directory):
        return None
    _validate_plugin_entries(directory)
    plugin = PluginResources(
        name=directory.name,
        directory=directory,
        mcp_sources=_discover_mcp_sources(directory / "mcp"),
        skill_directories=_discover_skill_directories(directory / "skills"),
    )
    if plugin.is_empty:
        return None
    if not plugin.mcp_sources or not plugin.skill_directories:
        raise PluginConventionError(
            f"plugin {plugin.name!r} must combine at least one MCP client "
            "module with at least one skill directory"
        )
    return plugin


def _is_runtime_plugin(directory: Path) -> bool:
    """Leave executable descriptors to the dedicated runtime-plugin loader."""

    manifest = directory / "plugin.yaml"
    if manifest.is_symlink():
        raise PluginConventionError(
            f"runtime plugin manifest cannot be a symlink: {manifest}"
        )
    if not manifest.exists():
        return False
    # Runtime plugins have a wider folder contract, but malformed descriptor
    # paths must not become a way to bypass either discovery implementation.
    if not manifest.is_file():
        raise PluginConventionError(
            f"runtime plugin manifest must be a regular file: {manifest}"
        )
    return True


def _validate_plugin_entries(directory: Path) -> None:
    """Explain the capability-plugin layout without confusing it with runtime plugins."""
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if path.is_symlink():
            raise PluginConventionError(f"plugin resource cannot be a symlink: {path}")
        if _is_ignored(path):
            continue
        if path.name not in {"mcp", "skills"} or not path.is_dir():
            raise PluginConventionError(
                authoring_guidance(
                    f"unexpected resource in plugin directory: {path}; plugins may contain only mcp/ and skills/",
                    expected="a plugin without plugin.yaml groups MCP clients in mcp/ and skills in skills/",
                    fix="Move client Python files into mcp/ and each skill into its own folder under skills/. "
                    "If you intended a runtime plugin, configure its plugin.yaml manifest instead. " + inactive_entry_hint(path),
                )
            )


def _discover_mcp_sources(directory: Path) -> tuple[Path, ...]:
    """Find plugin clients while explaining Python file and name requirements."""
    _require_optional_directory(directory, kind="plugin mcp")
    if not directory.exists():
        return ()

    sources: list[Path] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if path.is_symlink():
            raise PluginConventionError(
                f"plugin MCP resource cannot be a symlink: {path}"
            )
        if _is_ignored(path):
            continue
        if not path.is_file() or path.suffix != ".py" or not path.stem.isidentifier():
            raise PluginConventionError(
                authoring_guidance(
                    f"plugin MCP client must be a public Python module: {path}",
                    expected="a .py file with a Python-compatible name, defining client() that returns MCPClient",
                    fix="Use a filename such as search.py, without spaces or hyphens. " + inactive_entry_hint(path),
                )
            )
        sources.append(path)
    return tuple(sources)


def _discover_skill_directories(directory: Path) -> tuple[Path, ...]:
    """Find plugin skills and describe missing folders or instruction files."""
    _require_optional_directory(directory, kind="plugin skills")
    if not directory.exists():
        return ()

    skills: list[Path] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if path.is_symlink():
            raise PluginConventionError(
                f"plugin skill resource cannot be a symlink: {path}"
            )
        if _is_ignored(path):
            continue
        if not path.is_dir():
            raise PluginConventionError(
                folder_entry_error(f"plugin skill must be a directory containing SKILL.md: {path}", path, kind="skills")
            )
        manifest = path / "SKILL.md"
        if manifest.is_symlink():
            raise PluginConventionError(
                f"plugin skill manifest cannot be a symlink: {manifest}"
            )
        if not manifest.is_file():
            raise PluginConventionError(
                authoring_guidance(
                    f"plugin skill directory must contain uppercase SKILL.md: {path}",
                    expected="each skill folder contains its instructions in a file named exactly SKILL.md",
                    fix=f"Create {manifest}, or rename an existing skill.md to SKILL.md. " + inactive_entry_hint(path),
                )
            )
        _reject_tree_symlinks(path, kind="plugin skill")
        skills.append(path)
    return tuple(skills)


def _require_optional_directory(path: Path, *, kind: str) -> None:
    if path.is_symlink():
        raise PluginConventionError(f"{kind} directory cannot be a symlink: {path}")
    if path.exists() and not path.is_dir():
        raise PluginConventionError(f"{kind} path must be a directory: {path}")


def _reject_tree_symlinks(directory: Path, *, kind: str) -> None:
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise PluginConventionError(f"{kind} resource cannot be a symlink: {path}")


def _is_ignored(path: Path) -> bool:
    return (
        path.name in _IGNORED_NAMES
        or path.name.startswith(".")
        or path.name.startswith("_")
    )


__all__ = ["PluginConventionError", "PluginResources", "discover_plugins"]
