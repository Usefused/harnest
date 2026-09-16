"""Offline authoring contracts; provisioning is never an import side effect."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence, TYPE_CHECKING
from urllib.parse import urlsplit

from harnest.mcp import MCPClient

if TYPE_CHECKING:
    from .provision import FusedSetupResult


def _specifications(specs: Sequence[OpenAPISpec | str | Path]) -> tuple[OpenAPISpec, ...]:
    """Freeze a non-empty catalogue with one unambiguous identity per service."""

    declarations = tuple(item if isinstance(item, OpenAPISpec) else OpenAPISpec(item) for item in specs)
    if not declarations or len({item.name for item in declarations}) != len(declarations):
        raise ValueError("provide at least one spec, with a unique name for each spec")
    return declarations


def _slug(value: str) -> str:
    """Require a stable service identity that also fits local filenames."""

    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", value):
        raise ValueError("name must be a lowercase slug of at most 63 characters")
    return value


def _names(values: Sequence[str] | None, label: str) -> tuple[str, ...] | None:
    """Distinguish omitted selection from an accidentally empty selection."""

    if values is None:
        return None
    if isinstance(values, (str, bytes)) or not values:
        raise ValueError(f"{label} must be a non-empty sequence; omit it to use the default")
    result = tuple(values)
    if any(not isinstance(item, str) or not item.strip() for item in result):
        raise ValueError(f"{label} must contain non-empty strings")
    if len(set(result)) != len(result):
        raise ValueError(f"{label} must not contain duplicates")
    return result


def _environment_name(value: str) -> str:
    """Keep runtime credential references compatible with MCP expansion."""

    if not isinstance(value, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", value):
        raise ValueError("environment references must be uppercase variable names")
    return value


def _auth_selection(value: Mapping[str, str] | None) -> Mapping[str, str]:
    """Accept only Fused credential selectors, never provider credential values."""

    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping) or set(value) - {"type", "name", "ref"}:
        raise ValueError("auth supports only type, name, and optional ref")
    if not value.get("type") or not value.get("name"):
        raise ValueError("auth requires both type and name")
    if any(not isinstance(item, str) or not item.strip() for item in value.values()):
        raise ValueError("auth selectors must be non-empty strings")
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class OpenAPISpec:
    """One source service; omitted operations select its entire operation surface.

    ``version`` supplies the provider version only when the spec omits it.
    ``auth`` identifies credentials already managed in the chosen Fused bucket.
    Source paths resolve against the project passed to ``setup``.
    """

    source: str | Path = field(repr=False)
    name: str | None = None
    operations: Sequence[str] | None = None
    version: str | None = None
    auth: Mapping[str, str] | None = field(default=None, repr=False)
    scopes: Sequence[str] | None = None

    def __post_init__(self) -> None:
        """Freeze declarations without reading a file, environment, or network."""

        if not isinstance(self.source, (str, Path)) or not str(self.source).strip():
            raise ValueError("OpenAPI source must be a non-empty path or HTTP(S) URL")
        source = str(self.source)
        inferred = Path(urlsplit(source).path).stem.replace("_", "-")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "name", _slug(self.name if self.name is not None else inferred))
        object.__setattr__(self, "operations", _names(self.operations, "operations"))
        object.__setattr__(self, "scopes", _names(self.scopes, "scopes"))
        object.__setattr__(self, "auth", _auth_selection(self.auth))
        if self.version is not None and (not isinstance(self.version, str) or not self.version.strip()):
            raise ValueError("version must be a non-empty provider version")

    def selection(self, version: str) -> dict[str, Any]:
        """Project selection into Fused's single authoritative MCP config shape."""

        selected: dict[str, Any] = {"version": version}
        if self.operations is None:
            selected["select_all"] = True
        else:
            selected["operations"] = list(self.operations)
        if self.auth:
            selected["auth"] = dict(self.auth)
        if self.scopes:
            selected["connect"] = {"scopes": list(self.scopes)}
        return selected


@dataclass(frozen=True, slots=True)
class FusedMCPClient(MCPClient):
    """A normal MCP client carrying an optional, offline provisioning declaration."""

    specs: tuple[OpenAPISpec, ...] = field(default=(), repr=False)
    name: str = "fused"
    url_env: str = "HARNEST_FUSED_URL"
    token_env: str = "HARNEST_FUSED_TOKEN"

    @classmethod
    def from_openapi(
        cls, *specs: OpenAPISpec | str | Path, name: str,
        url_env: str | None = None, token_env: str | None = None,
        **client_options: Any,
    ) -> FusedMCPClient:
        """Declare one MCP connection spanning all specs without provisioning it.

        Strings are shorthand for ``OpenAPISpec`` with all operations selected.
        ``client_options`` are the standard ``MCPClient.streamable_http`` options.
        Authorization always uses an execution-token environment reference.
        """

        from dataclasses import replace

        name = _slug(name)
        declarations = _specifications(specs)
        base = f"HARNEST_FUSED_{name.replace('-', '_').upper()}"
        url_env = _environment_name(url_env if url_env is not None else f"{base}_URL")
        token_env = _environment_name(token_env if token_env is not None else f"{base}_TOKEN")
        if url_env == token_env:
            raise ValueError("URL and token must use different environment variables")
        headers = dict(client_options.pop("headers", {}))
        if any(key.lower() == "authorization" for key in headers):
            raise ValueError("use token_env instead of an Authorization header")
        headers["Authorization"] = f"Bearer ${{{token_env}}}"
        client = cls.streamable_http(f"${{{url_env}}}", headers=headers, **client_options)
        return replace(client, specs=declarations, name=name, url_env=url_env, token_env=token_env)

    def setup(
        self, *, description: str, bucket: str, version: str = "1.0.0",
        project: str | Path = ".", cli: str = "fused-cli",
        timeout_seconds: float = 1260,
    ) -> FusedSetupResult:
        """Explicitly import specs and provision the server using the user's CLI.

        This is a developer setup operation, never a runtime lifecycle hook.
        The CLI displays its one-time execution token directly to the operator.
        """

        from .provision import setup

        return setup(self, description=description, bucket=bucket, version=version,
                     project=project, cli=cli, timeout_seconds=timeout_seconds)
