"""Validated configuration for the server embedded in a compiled agent."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from pathlib import Path
import re
import shutil
import stat
from typing import Any, Mapping

import yaml


SERVER_CONFIG_FILENAME = "server.yaml"
_API_VERSION = "harnest.dev/v1alpha1"
_MAX_CONFIG_BYTES = 64 * 1024
_MAX_REQUEST_BYTES = 1024 * 1024 * 1024
_SIZE_PATTERN = re.compile(r"^([1-9][0-9]*)(B|KiB|MiB|GiB)?$")
_SIZE_MULTIPLIERS = {
    None: 1,
    "B": 1,
    "KiB": 1024,
    "MiB": 1024 * 1024,
    "GiB": 1024 * 1024 * 1024,
}


class ServerConfigError(ValueError):
    """A server.yaml file is unsafe or does not match the public contract."""


@dataclass(frozen=True, slots=True)
class HTTPServerConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    allow_remote: bool = False
    request_timeout_seconds: float = 300
    max_concurrent_requests: int = 8


@dataclass(frozen=True, slots=True)
class ServerLimits:
    max_request_bytes: int = 1024 * 1024


@dataclass(frozen=True, slots=True)
class PlaygroundConfig:
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class ServerConfig:
    http: HTTPServerConfig = HTTPServerConfig()
    limits: ServerLimits = ServerLimits()
    playground: PlaygroundConfig = PlaygroundConfig()

    def with_overrides(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        request_timeout_seconds: float | None = None,
        max_concurrent_requests: int | None = None,
        allow_remote: bool | None = None,
    ) -> "ServerConfig":
        """Apply the compiled launcher's explicit operational overrides."""

        http = replace(
            self.http,
            host=self.http.host if host is None else host,
            port=self.http.port if port is None else port,
            request_timeout_seconds=(
                self.http.request_timeout_seconds
                if request_timeout_seconds is None
                else request_timeout_seconds
            ),
            max_concurrent_requests=(
                self.http.max_concurrent_requests
                if max_concurrent_requests is None
                else max_concurrent_requests
            ),
            allow_remote=self.http.allow_remote if allow_remote is None else allow_remote,
        )
        return replace(self, http=_validate_http(http))


DEFAULT_SERVER_CONFIG = ServerConfig()
DEFAULT_SERVER_YAML = """apiVersion: harnest.dev/v1alpha1
kind: Server
http:
  host: 127.0.0.1
  port: 8080
  allowRemote: false
  requestTimeoutSeconds: 300
  maxConcurrentRequests: 8
limits:
  maxRequestBytes: 1MiB
playground:
  enabled: true
"""


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: Any, node: Any, deep: bool = False) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _value_node in node.value:
        key = loader.construct_object(key_node, deep=False)
        if not isinstance(key, str):
            raise ServerConfigError("server.yaml mapping keys must be strings")
        if key in seen:
            raise ServerConfigError(f"duplicate server.yaml key: {key}")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def load_server_config(path: str | Path) -> ServerConfig:
    """Load one required server.yaml without following a symlink."""

    config_path = Path(path)
    contents = _read_config(config_path)
    try:
        value = yaml.load(contents, Loader=_UniqueKeyLoader)
    except (ServerConfigError, yaml.YAMLError) as exc:
        raise ServerConfigError(f"invalid {config_path}: {exc}") from exc
    return _decode_config(value, config_path)


def materialize_server_config(source: str | Path, destination: str | Path) -> ServerConfig:
    """Validate authored policy and place its mutable copy beside the launcher."""

    source_path = Path(source)
    destination_path = Path(destination)
    if source_path.exists() or source_path.is_symlink():
        config = load_server_config(source_path)
        shutil.copyfile(source_path, destination_path)
        return config
    destination_path.write_text(DEFAULT_SERVER_YAML, encoding="utf-8")
    return DEFAULT_SERVER_CONFIG


def format_byte_size(value: int) -> str:
    for suffix, multiplier in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if value % multiplier == 0:
            return f"{value // multiplier}{suffix}"
    return f"{value}B"


def validate_max_request_bytes(value: Any) -> int:
    """Apply the same safety ceiling to YAML and embedding API values."""

    return _byte_size(value, "max_request_bytes")


def _read_config(path: Path) -> str:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ServerConfigError(f"read {path}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ServerConfigError(f"server configuration must be a regular file: {path}")
    if info.st_size > _MAX_CONFIG_BYTES:
        raise ServerConfigError(f"server configuration exceeds 64KiB: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ServerConfigError(f"read {path}: {exc}") from exc


def _decode_config(value: Any, path: Path) -> ServerConfig:
    root = _mapping(value, "server.yaml")
    _require_keys(root, {"apiVersion", "kind", "http", "limits", "playground"}, "server.yaml")
    if root["apiVersion"] != _API_VERSION or root["kind"] != "Server":
        raise ServerConfigError(
            f"{path}: unsupported apiVersion/kind "
            f"{root['apiVersion']!r}/{root['kind']!r}"
        )
    return ServerConfig(
        http=_decode_http(root["http"]),
        limits=_decode_limits(root["limits"]),
        playground=_decode_playground(root["playground"]),
    )


def _decode_http(value: Any) -> HTTPServerConfig:
    data = _mapping(value, "http")
    _require_keys(
        data,
        {
            "host",
            "port",
            "allowRemote",
            "requestTimeoutSeconds",
            "maxConcurrentRequests",
        },
        "http",
    )
    return _validate_http(
        HTTPServerConfig(
            host=_text(data["host"], "http.host"),
            port=_integer(data["port"], "http.port", minimum=1, maximum=65535),
            allow_remote=_boolean(data["allowRemote"], "http.allowRemote"),
            request_timeout_seconds=_number(
                data["requestTimeoutSeconds"],
                "http.requestTimeoutSeconds",
                maximum=86400,
            ),
            max_concurrent_requests=_integer(
                data["maxConcurrentRequests"],
                "http.maxConcurrentRequests",
                minimum=1,
                maximum=100000,
            ),
        )
    )


def _validate_http(config: HTTPServerConfig) -> HTTPServerConfig:
    host = _text(config.host, "http.host")
    port = _integer(config.port, "http.port", minimum=1, maximum=65535)
    timeout = _number(
        config.request_timeout_seconds,
        "http.requestTimeoutSeconds",
        maximum=86400,
    )
    concurrency = _integer(
        config.max_concurrent_requests,
        "http.maxConcurrentRequests",
        minimum=1,
        maximum=100000,
    )
    allow_remote = _boolean(config.allow_remote, "http.allowRemote")
    return HTTPServerConfig(host, port, allow_remote, timeout, concurrency)


def _decode_limits(value: Any) -> ServerLimits:
    data = _mapping(value, "limits")
    _require_keys(data, {"maxRequestBytes"}, "limits")
    return ServerLimits(_byte_size(data["maxRequestBytes"], "limits.maxRequestBytes"))


def _decode_playground(value: Any) -> PlaygroundConfig:
    data = _mapping(value, "playground")
    _require_keys(data, {"enabled"}, "playground")
    return PlaygroundConfig(_boolean(data["enabled"], "playground.enabled"))


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ServerConfigError(f"{name} must be a mapping with string keys")
    return value


def _require_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        detail = []
        if missing:
            detail.append(f"missing {missing}")
        if unknown:
            detail.append(f"unknown {unknown}")
        raise ServerConfigError(f"{name} fields are invalid: {', '.join(detail)}")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ServerConfigError(f"{name} must be a non-empty string")
    return value.strip()


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ServerConfigError(f"{name} must be boolean")
    return value


def _integer(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ServerConfigError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise ServerConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _number(value: Any, name: str, *, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ServerConfigError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or result <= 0 or result > maximum:
        raise ServerConfigError(f"{name} must be greater than zero and at most {maximum:g}")
    return result


def _byte_size(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ServerConfigError(f"{name} must be bytes or a binary size such as 10MiB")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and (match := _SIZE_PATTERN.fullmatch(value)):
        result = int(match.group(1)) * _SIZE_MULTIPLIERS[match.group(2)]
    else:
        raise ServerConfigError(f"{name} must be bytes or a binary size such as 10MiB")
    if result < 1024 or result > _MAX_REQUEST_BYTES:
        raise ServerConfigError(f"{name} must be between 1KiB and 1GiB")
    return result


__all__ = [
    "DEFAULT_SERVER_CONFIG",
    "DEFAULT_SERVER_YAML",
    "HTTPServerConfig",
    "PlaygroundConfig",
    "SERVER_CONFIG_FILENAME",
    "ServerConfig",
    "ServerConfigError",
    "ServerLimits",
    "format_byte_size",
    "load_server_config",
    "materialize_server_config",
    "validate_max_request_bytes",
]
