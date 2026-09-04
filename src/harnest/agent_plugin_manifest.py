"""Local Agent Plugins 1.0 validation; never fetch schemas or execute packages."""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit
import warnings

PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
_NAME = re.compile(r"(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\Z")
_HEADER = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_METADATA = {"version", "description", "homepage", "repository", "license"}
_FIELDS = _METADATA | {"$schema", "name", "author", "keywords", "extensions"}
_MAX_JSON_BYTES = 1024 * 1024


class AgentPluginError(ValueError):
    """An invalid portable component must not disable independent components."""


def diagnostic(package: Path, detail: str) -> None:
    """Report actionable package failures without echoing configured secret values."""
    warnings.warn(f"Agent Plugin {package.name!r}: {detail}", UserWarning, stacklevel=2)


def contained(root: Path, path: Path) -> Path:
    """Resolve package-controlled paths without permitting filesystem escapes."""
    try:
        resolved = path.resolve()
        boundary = root.resolve()
    except (OSError, RuntimeError) as error:
        raise AgentPluginError("a package path cannot be safely resolved") from error
    if not resolved.is_relative_to(boundary):
        raise AgentPluginError("a package path resolves outside the plugin folder")
    return resolved


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous JSON instead of letting the last duplicate key win."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise AgentPluginError("JSON contains a duplicate field")
        result[key] = value
    return result


def read_object(root: Path, filename: str) -> dict[str, Any]:
    """Read bounded, strict JSON within the package, without network resolution."""
    path = contained(root, root / filename)
    if not path.is_file():
        raise AgentPluginError(f"{filename} must be a regular JSON file")
    with path.open("rb") as stream:
        raw = stream.read(_MAX_JSON_BYTES + 1)
    if len(raw) > _MAX_JSON_BYTES:
        raise AgentPluginError(f"{filename} exceeds the 1 MiB limit")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_pairs,
                           parse_constant=lambda _: _invalid_constant())
    except (ValueError, UnicodeError, RecursionError) as error:
        raise AgentPluginError(f"{filename} must contain valid JSON with unique fields") from error
    if not isinstance(value, dict):
        raise AgentPluginError(f"{filename} must contain a JSON object")
    return value


def _invalid_constant() -> None:
    """Reject non-JSON NaN and infinity values accepted by Python's decoder."""
    raise AgentPluginError("non-JSON numeric constant")


def load_manifest(root: Path) -> dict[str, Any]:
    """Apply the standard's fatal schema checks and nonfatal unknown-field rules."""
    value = read_object(root, "plugin.json")
    if value.get("$schema") != PLUGIN_SCHEMA:
        raise AgentPluginError(f"plugin.json must declare $schema {PLUGIN_SCHEMA}")
    name = value.get("name")
    if not isinstance(name, str) or not 1 <= len(name) <= 64 or not _NAME.fullmatch(name):
        raise AgentPluginError("plugin.json name must use 1–64 lowercase letters, digits, hyphens or periods, without consecutive hyphens or periods")
    if value.keys() - _FIELDS:
        diagnostic(root, "ignoring unknown plugin.json fields; client-specific data belongs under extensions")
    _validate_metadata(value)
    if "extensions" in value and not isinstance(value["extensions"], dict):
        diagnostic(root, "ignoring extensions: expected a JSON object")
    # Unimplemented namespace contents have no Harnest semantics, even if malformed.
    return {key: item for key, item in value.items() if key in _FIELDS - {"extensions"}}


def _validate_metadata(value: dict[str, Any]) -> None:
    """Validate metadata types without adding URL, email, or SemVer restrictions."""
    if any(not isinstance(value[key], str) for key in _METADATA & value.keys()):
        raise AgentPluginError("plugin metadata values must be strings")
    if "author" in value:
        author = value["author"]
        if not isinstance(author, dict) or author.keys() - {"name", "email", "url"}:
            raise AgentPluginError("author must be an object containing only name, email and url")
        string_map(author, "author")
    if "keywords" in value:
        string_list(value["keywords"], "keywords")


def string_map(value: Any, label: str) -> None:
    """Validate JSON string maps without including their values in diagnostics."""
    if not isinstance(value, dict) or any(not isinstance(item, str) for item in value.values()):
        raise AgentPluginError(f"{label} must be an object of strings")


def string_list(value: Any, label: str) -> None:
    """Validate ordered argument and keyword lists without value disclosure."""
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AgentPluginError(f"{label} must be a list of strings")


def load_servers(root: Path) -> tuple[tuple[str, dict[str, Any]], ...]:
    """Disable invalid MCP configuration or individual entries, retaining skills."""
    path = root / "mcp.json"
    if not path.exists() and not path.is_symlink():
        return ()
    try:
        value = read_object(root, "mcp.json")
        if set(value) != {"$schema", "mcpServers"} or value.get("$schema") != MCP_SCHEMA:
            raise AgentPluginError(f"mcp.json must contain $schema {MCP_SCHEMA} and mcpServers only")
        if not isinstance(value["mcpServers"], dict):
            raise AgentPluginError("mcpServers must be an object")
    except (AgentPluginError, OSError) as error:
        diagnostic(root, f"MCP disabled: {error if isinstance(error, AgentPluginError) else 'cannot read mcp.json'}; valid skills remain available")
        return ()
    return tuple(entry for name, server in value["mcpServers"].items()
                 if (entry := _server_entry(root, name, server)) is not None)


def _server_entry(root: Path, name: str, value: Any) -> tuple[str, dict[str, Any]] | None:
    """Keep malformed server entries inside their own diagnostic boundary."""
    try:
        # Server names become runtime identities; JSON surrogate escapes must not
        # escape the per-entry failure boundary during UTF-8 hashing or logging.
        name.encode("utf-8")
        validate_server(root, value)
    except (AgentPluginError, OSError, ValueError) as error:
        detail = str(error) if isinstance(error, AgentPluginError) else "invalid server configuration"
        diagnostic(root, f"MCP server {name!r} skipped: {detail}")
        return None
    return name, value


def validate_server(root: Path, value: Any) -> None:
    """Select the explicit transport without guessing or falling back."""
    if not isinstance(value, dict):
        raise AgentPluginError("server configuration must be an object")
    transport = value.get("type")
    if not isinstance(transport, str):
        raise AgentPluginError("server type must be a string")
    if transport == "stdio":
        _validate_stdio(root, value)
    elif transport in {"streamable-http", "sse"}:
        _validate_http(value)
    else:
        raise AgentPluginError("unsupported type; use stdio, streamable-http or sse")


def _validate_stdio(root: Path, value: dict[str, Any]) -> None:
    """Keep executable tokens, working directories, and reserved variables explicit."""
    if value.keys() - {"type", "command", "args", "env", "cwd"}:
        raise AgentPluginError("stdio server has unsupported fields")
    command = value.get("command")
    if not isinstance(command, str) or not command or "\x00" in command:
        raise AgentPluginError("command must be one executable name or a ./plugin-relative path")
    _validate_command(root, command)
    string_list(value.get("args", []), "args")
    env = value.get("env", {})
    string_map(env, "env")
    if any(key.upper() in {"PLUGIN_ROOT", "PLUGIN_DATA"} for key in env):
        raise AgentPluginError("PLUGIN_ROOT and PLUGIN_DATA are supplied by Harnest, not env")
    validate_cwd(root, value.get("cwd", "./"))


def _validate_command(root: Path, command: str) -> None:
    """Do not interpret a shell command string or expand executable placeholders."""
    if command.startswith("./"):
        contained(root, root / command)
        return
    if any(char.isspace() for char in command) or any(char in command for char in "/\\"):
        raise AgentPluginError("command must be one bare executable or a ./plugin-relative path, not a shell command")


def validate_cwd(root: Path, value: Any) -> None:
    """Validate package-relative paths now and data-relative paths again at runtime."""
    if not isinstance(value, str) or "\x00" in value:
        raise AgentPluginError("cwd must be a plugin-relative or plugin-variable-rooted path")
    if value.startswith("./"):
        contained(root, root / value)
    elif value == "${PLUGIN_ROOT}" or value.startswith("${PLUGIN_ROOT}/"):
        contained(root, root / value.removeprefix("${PLUGIN_ROOT}").lstrip("/"))
    elif value == "${PLUGIN_DATA}" or value.startswith("${PLUGIN_DATA}/"):
        # Lexical validation avoids creating persistent state during compilation.
        contained(root, root / value.removeprefix("${PLUGIN_DATA}").lstrip("/"))
    else:
        raise AgentPluginError("cwd must begin with ./, ${PLUGIN_ROOT} or ${PLUGIN_DATA}")


def _validate_http(value: dict[str, Any]) -> None:
    """Require safe endpoint schemes and literal, non-ambiguous HTTP headers."""
    if value.keys() - {"type", "url", "headers"}:
        raise AgentPluginError("HTTP server has unsupported fields")
    url = value.get("url")
    _validate_url_text(url)
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or "#" in url:
        raise AgentPluginError("url must be HTTP(S), without user information or a fragment")
    if parsed.scheme == "http" and not _loopback(parsed.hostname):
        raise AgentPluginError("non-loopback MCP servers require HTTPS")
    _ = parsed.port
    headers = value.get("headers", {})
    string_map(headers, "headers")
    _validate_headers(headers)


def _validate_url_text(url: Any) -> None:
    """Reject characters URL parsers silently strip before origin validation."""
    if not isinstance(url, str) or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
        raise AgentPluginError("url must be an absolute HTTP or HTTPS URL without whitespace or control characters")


def _loopback(host: str) -> bool:
    """Do not trust DNS resolution to turn a remote hostname into loopback."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_headers(headers: dict[str, str]) -> None:
    """Prevent header splitting and duplicates disguised by case differences."""
    seen = set()
    for key, value in headers.items():
        if not _HEADER.fullmatch(key) or key.lower() in seen:
            raise AgentPluginError("HTTP header names must be valid and unique ignoring case")
        if any(ord(char) < 32 and char != "\t" or ord(char) == 127 or ord(char) > 255 for char in value):
            raise AgentPluginError("HTTP header values must not contain control characters")
        seen.add(key.lower())
