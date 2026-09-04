"""Validated configuration for the server embedded in a compiled agent."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Any, Callable, Mapping

import yaml


SERVER_CONFIG_FILENAME = "server.yaml"
_API_VERSION = "harnest.dev/v1alpha1"
_MAX_CONFIG_BYTES = 64 * 1024
_MAX_REQUEST_BYTES = 1024 * 1024 * 1024
_ENVIRONMENT_REFERENCE_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_SIZE_PATTERN = re.compile(r"^([1-9][0-9]*)(B|KiB|MiB|GiB)?$")
_SIZE_MULTIPLIERS = {
    None: 1,
    "B": 1,
    "KiB": 1024,
    "MiB": 1024 * 1024,
    "GiB": 1024 * 1024 * 1024,
}


class ServerConfigError(ValueError):
    """Server settings are unsafe or do not match the public contract."""


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
    live: bool = False

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
live: false
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
    """Reject ambiguous keys in both project and legacy server documents."""

    seen: set[Any] = set()
    for key_node, _value_node in node.value:
        key = loader.construct_object(key_node, deep=False)
        # Restrict keys before set membership so complex YAML keys fail cleanly.
        if not isinstance(key, str):
            raise ServerConfigError("configuration mapping keys must be strings")
        # Never let YAML silently choose the last security-sensitive setting.
        if key in seen:
            raise ServerConfigError(f"duplicate configuration key: {key}")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def load_server_config(
    path: str | Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> ServerConfig:
    """Load server.yaml and resolve exact environment references."""

    effective_environment = os.environ if environment is None else environment
    return _load_server_config(path, effective_environment)


def validate_server_config_template(path: str | Path) -> None:
    """Validate authored structure without resolving deployment environment."""

    _load_server_config(path, None)


def _load_server_config(
    path: str | Path,
    environment: Mapping[str, str] | None,
) -> ServerConfig:
    """Read once so compilation and startup share parsing and validation."""

    config_path = Path(path)
    return _decode_config(_read_yaml(config_path), config_path, environment)


def _read_yaml(path: Path, *, max_bytes: int | None = _MAX_CONFIG_BYTES) -> Any:
    """Read regular YAML with the owning document's size and key policy."""

    contents = _read_config(path, max_bytes=max_bytes)
    try:
        return yaml.load(contents, Loader=_UniqueKeyLoader)
    except (ServerConfigError, yaml.YAMLError) as exc:
        raise ServerConfigError(f"invalid {path}: {exc}") from exc


def project_server_config_yaml(directory: str | Path) -> str:
    """Validate one authored server source and preserve startup-only references."""

    root = Path(directory)
    project = root / "config.yaml"
    legacy = root / SERVER_CONFIG_FILENAME
    # Direct Python compilation may have no deployment config; it still gets defaults.
    if project.exists() or project.is_symlink():
        # Deployment config historically has no server-file size limit; apply
        # that limit to the extracted runtime document, not unrelated settings.
        config = _mapping(_read_yaml(project, max_bytes=None), str(project))
        # Presence, including an empty section, claims sole ownership of server policy.
        if "server" in config:
            if legacy.exists() or legacy.is_symlink():
                raise ServerConfigError(
                    "choose config.yaml server or legacy server.yaml, not both; "
                    "move the legacy settings into server and remove server.yaml"
                )
            document = _project_server_document(config["server"])
            _decode_config(document, project, None)
            contents = yaml.safe_dump(document, sort_keys=False)
            # The generated file must fit the same bound enforced at startup.
            if len(contents.encode("utf-8")) > _MAX_CONFIG_BYTES:
                raise ServerConfigError(f"server configuration exceeds 64KiB: {project}")
            return contents
    # Preserve legacy bytes and their strict versioned contract for existing projects.
    if legacy.exists() or legacy.is_symlink():
        validate_server_config_template(legacy)
        return _read_config(legacy)
    return DEFAULT_SERVER_YAML


def _project_server_document(value: Any) -> dict[str, Any]:
    """Fill omitted fields without resolving environment values or hiding typos."""

    settings = _mapping(value, "config.yaml server")
    document = yaml.safe_load(DEFAULT_SERVER_YAML)
    sections = {"http", "limits", "playground", "live"}
    _require_keys(dict.fromkeys(sections) | dict(settings), sections, "server")
    for name, value in settings.items():
        # Live is a transport switch, while the other sections merge nested defaults.
        if name == "live":
            document[name] = value
            continue
        overrides = _mapping(value, f"server.{name}")
        document[name] = document[name] | dict(overrides)
    return document


def materialize_server_config(source: str | Path, destination: str | Path) -> None:
    """Validate authored policy and place its mutable copy beside the launcher."""

    source_path = Path(source)
    destination_path = Path(destination)
    if source_path.exists() or source_path.is_symlink():
        # Compilation must preserve references for the deployment environment;
        # only the standalone launcher is allowed to resolve their values.
        validate_server_config_template(source_path)
        shutil.copyfile(source_path, destination_path)
        return
    destination_path.write_text(DEFAULT_SERVER_YAML, encoding="utf-8")


def format_byte_size(value: int) -> str:
    for suffix, multiplier in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if value % multiplier == 0:
            return f"{value // multiplier}{suffix}"
    return f"{value}B"


def validate_max_request_bytes(value: Any) -> int:
    """Apply the same safety ceiling to YAML and embedding API values."""

    return _byte_size(value, "max_request_bytes")


def _read_config(path: Path, *, max_bytes: int | None = _MAX_CONFIG_BYTES) -> str:
    """Require a regular UTF-8 file and enforce the server document size bound."""

    try:
        info = path.lstat()
    except OSError as exc:
        raise ServerConfigError(f"read {path}: {exc}") from exc
    # Never follow authored links or read special files as server policy.
    if not stat.S_ISREG(info.st_mode):
        raise ServerConfigError(f"server configuration must be a regular file: {path}")
    # Project files may be larger; only the extracted server document is bounded.
    if max_bytes is not None and info.st_size > max_bytes:
        raise ServerConfigError(f"server configuration exceeds 64KiB: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ServerConfigError(f"read {path}: {exc}") from exc


def _decode_config(
    value: Any,
    path: Path,
    environment: Mapping[str, str] | None,
) -> ServerConfig:
    """Validate current policy while retaining live access for legacy documents."""

    # Older compiled/authored server files exposed WebSockets unconditionally.
    # New defaults explicitly include live: false and do not take this fallback.
    root = {"live": True, **_mapping(value, "server.yaml")}
    _require_keys(root, {"apiVersion", "kind", "http", "limits", "playground", "live"}, "server.yaml")
    # Reject unrelated document kinds before decoding their settings.
    if root["apiVersion"] != _API_VERSION or root["kind"] != "Server":
        raise ServerConfigError(
            f"{path}: unsupported apiVersion/kind "
            f"{root['apiVersion']!r}/{root['kind']!r}"
        )
    return ServerConfig(
        http=_decode_http(root["http"], environment),
        limits=_decode_limits(root["limits"], environment),
        playground=_decode_playground(root["playground"], environment),
        live=_resolved_boolean(root["live"], "live", environment),
    )


def _decode_http(
    value: Any,
    environment: Mapping[str, str] | None,
) -> HTTPServerConfig:
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
            host=_resolved_text(data["host"], "http.host", environment),
            port=_resolved_integer(
                data["port"],
                "http.port",
                environment,
                minimum=1,
                maximum=65535,
            ),
            allow_remote=_resolved_boolean(
                data["allowRemote"], "http.allowRemote", environment
            ),
            request_timeout_seconds=_resolved_number(
                data["requestTimeoutSeconds"],
                "http.requestTimeoutSeconds",
                environment,
                maximum=86400,
            ),
            max_concurrent_requests=_resolved_integer(
                data["maxConcurrentRequests"],
                "http.maxConcurrentRequests",
                environment,
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


def _decode_limits(
    value: Any,
    environment: Mapping[str, str] | None,
) -> ServerLimits:
    data = _mapping(value, "limits")
    _require_keys(data, {"maxRequestBytes"}, "limits")
    return ServerLimits(
        _resolved_byte_size(
            data["maxRequestBytes"], "limits.maxRequestBytes", environment
        )
    )


def _decode_playground(
    value: Any,
    environment: Mapping[str, str] | None,
) -> PlaygroundConfig:
    data = _mapping(value, "playground")
    _require_keys(data, {"enabled"}, "playground")
    return PlaygroundConfig(
        _resolved_boolean(data["enabled"], "playground.enabled", environment)
    )


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


def _resolve_scalar(
    value: Any,
    name: str,
    environment: Mapping[str, str] | None,
    *,
    placeholder: Any,
    expected: str,
    parser: Callable[[Any, bool], Any],
) -> Any:
    reference = (
        _ENVIRONMENT_REFERENCE_PATTERN.fullmatch(value)
        if isinstance(value, str)
        else None
    )
    if reference is None:
        if isinstance(value, str) and "$" in value:
            raise ServerConfigError(
                f"{name} environment references must use exact ${{NAME}} syntax"
            )
        return parser(value, False)

    variable = reference.group(1)
    if environment is None:
        return placeholder
    if variable not in environment:
        raise ServerConfigError(
            f"environment variable {variable} required by {name} is unset"
        )
    raw_value = environment[variable]
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ServerConfigError(
            f"environment variable {variable} required by {name} is empty"
        )
    try:
        return parser(raw_value.strip(), True)
    except (TypeError, ValueError):
        # Environment values may contain credentials or gateway details, so a
        # diagnostic identifies only the variable and destination field.
        raise ServerConfigError(
            f"environment variable {variable} is invalid for {name}; expected {expected}"
        ) from None


def _resolved_text(
    value: Any,
    name: str,
    environment: Mapping[str, str] | None,
) -> str:
    return _resolve_scalar(
        value,
        name,
        environment,
        placeholder="environment-reference",
        expected="a non-empty string",
        parser=lambda item, _from_environment: _text(item, name),
    )


def _resolved_boolean(
    value: Any,
    name: str,
    environment: Mapping[str, str] | None,
) -> bool:
    return _resolve_scalar(
        value,
        name,
        environment,
        placeholder=False,
        expected="true or false",
        parser=lambda item, from_environment: _boolean(
            _environment_boolean(item) if from_environment else item,
            name,
        ),
    )


def _resolved_integer(
    value: Any,
    name: str,
    environment: Mapping[str, str] | None,
    *,
    minimum: int,
    maximum: int,
) -> int:
    return _resolve_scalar(
        value,
        name,
        environment,
        placeholder=minimum,
        expected=f"an integer between {minimum} and {maximum}",
        parser=lambda item, from_environment: _integer(
            _environment_integer(item) if from_environment else item,
            name,
            minimum=minimum,
            maximum=maximum,
        ),
    )


def _resolved_number(
    value: Any,
    name: str,
    environment: Mapping[str, str] | None,
    *,
    maximum: float,
) -> float:
    return _resolve_scalar(
        value,
        name,
        environment,
        placeholder=1.0,
        expected=f"a number greater than zero and at most {maximum:g}",
        parser=lambda item, from_environment: _number(
            _environment_number(item) if from_environment else item,
            name,
            maximum=maximum,
        ),
    )


def _resolved_byte_size(
    value: Any,
    name: str,
    environment: Mapping[str, str] | None,
) -> int:
    return _resolve_scalar(
        value,
        name,
        environment,
        placeholder=1024,
        expected="bytes between 1KiB and 1GiB",
        parser=lambda item, _from_environment: _byte_size(item, name),
    )


def _environment_boolean(value: Any) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError("invalid environment boolean")


def _environment_integer(value: Any) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
        raise ValueError("invalid environment integer")
    return int(value)


def _environment_number(value: Any) -> float:
    if not isinstance(value, str):
        raise ValueError("invalid environment number")
    return float(value)


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
    "project_server_config_yaml",
    "validate_server_config_template",
    "validate_max_request_bytes",
]
