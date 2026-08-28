"""Framework boundary objects used by compiled Harnest artifacts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .agent import _AdvancedAgentDefinition
from .assets import AssetStore
from .checkpoint import ADKStore, CheckpointAuthority
from .context import ContextValue
from .credentials import CredentialProvider
from .http_routes import HTTPRouteExtension
from .lifecycle import LifecycleListener
from .output import OutputPolicy
from .session import SessionStore
from .structured import PydanticModel, validate_output_schema


@dataclass(frozen=True, slots=True)
class RuntimeCapabilities:
    """Validated application-owned resources consumed by every runtime."""

    session_store: SessionStore | ADKStore | None = None
    checkpointer: CheckpointAuthority | None = None
    asset_store: AssetStore | None = field(default=None, repr=False)
    credential_provider: CredentialProvider | None = field(default=None, repr=False)
    http_routes: Sequence[HTTPRouteExtension] = field(default=(), repr=False)
    output_policy: OutputPolicy = OutputPolicy()
    telemetry_exporters: Sequence[LifecycleListener] = field(default=(), repr=False)
    context_values: Sequence[ContextValue] = ()

    def __post_init__(self) -> None:
        """Validate capability contracts and freeze repeatable collections."""

        _validate_session_store(self.session_store)
        _validate_optional_type(
            self.checkpointer, CheckpointAuthority, field_name="checkpointer"
        )
        _validate_optional_type(
            self.asset_store, AssetStore, field_name="asset_store"
        )
        _validate_optional_type(
            self.credential_provider,
            CredentialProvider,
            field_name="credential_provider",
        )
        if not isinstance(self.output_policy, OutputPolicy):
            raise TypeError("output_policy must be OutputPolicy")
        object.__setattr__(
            self, "http_routes", _http_route_extensions(self.http_routes)
        )
        object.__setattr__(
            self,
            "telemetry_exporters",
            _telemetry_exporter_factories(self.telemetry_exporters),
        )
        object.__setattr__(
            self, "context_values", _context_capabilities(self.context_values)
        )


@dataclass(frozen=True, slots=True)
class CompiledApplication:
    """The framework-neutral object exported by every generated artifact."""

    name: str
    framework: str
    mode: str
    target: Any
    native_app: Any | None = None
    kind: str = "agent"
    bridge: _AdvancedAgentDefinition | None = None
    extensions: Sequence[Any] = ()
    session_store: SessionStore | ADKStore | None = None
    checkpointer: CheckpointAuthority | None = None
    asset_store: AssetStore | None = field(default=None, repr=False)
    credential_provider: CredentialProvider | None = field(default=None, repr=False)
    http_routes: Sequence[HTTPRouteExtension] = field(default=(), repr=False)
    output_policy: OutputPolicy = OutputPolicy()
    telemetry_exporters: Sequence[Any] = field(default=(), repr=False)
    input_schema: PydanticModel | None = None
    output_schema: PydanticModel | None = None
    checkpoint_metadata: dict[str, str] | None = None
    context_values: Sequence[ContextValue] = ()
    harnest_version: str | None = None
    framework_distribution: str | None = None
    framework_version: str | None = None
    runtime_capabilities: RuntimeCapabilities = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        """Freeze normalized metadata at the compiled framework boundary."""

        if self.framework not in {"adk", "langgraph"}:
            raise ValueError(f"unsupported compiled framework: {self.framework}")
        if self.mode not in {"managed", "advanced"}:
            raise ValueError(f"unsupported compiled mode: {self.mode}")
        if self.kind not in {"agent", "graph", "advanced"}:
            raise ValueError(f"unsupported compiled application kind: {self.kind}")
        capabilities = RuntimeCapabilities(
            session_store=self.session_store,
            checkpointer=self.checkpointer,
            asset_store=self.asset_store,
            credential_provider=self.credential_provider,
            http_routes=self.http_routes,
            output_policy=self.output_policy,
            telemetry_exporters=self.telemetry_exporters,
            context_values=self.context_values,
        )
        object.__setattr__(self, "runtime_capabilities", capabilities)
        _publish_compatibility_attributes(self, capabilities)
        object.__setattr__(self, "extensions", tuple(self.extensions))
        object.__setattr__(
            self, "checkpoint_metadata", dict(self.checkpoint_metadata or {})
        )
        validate_output_schema(
            self.input_schema, field_name="compiled application input_schema"
        )
        validate_output_schema(
            self.output_schema, field_name="compiled application output_schema"
        )


def _publish_compatibility_attributes(
    application: CompiledApplication, capabilities: RuntimeCapabilities
) -> None:
    """Keep legacy fields as aliases while runtimes migrate to the grouping."""

    # The aliases preserve imported artifact behavior and direct application
    # construction without allowing validation to diverge between transports.
    for name in (
        "session_store",
        "checkpointer",
        "asset_store",
        "credential_provider",
        "http_routes",
        "output_policy",
        "telemetry_exporters",
        "context_values",
    ):
        object.__setattr__(application, name, getattr(capabilities, name))


def _validate_session_store(value: Any) -> None:
    """Accept the portable protocol and ADK's explicit native wrapper."""

    if value is not None and not isinstance(value, (SessionStore, ADKStore)):
        raise TypeError("session_store must implement SessionStore or be ADKStore")


def _validate_optional_type(value: Any, expected: type[Any], *, field_name: str) -> None:
    """Validate one optional capability without initializing external resources."""

    if value is not None and not isinstance(value, expected):
        raise TypeError(f"{field_name} must implement {expected.__name__}")


def _http_route_extensions(values: Sequence[Any]) -> tuple[HTTPRouteExtension, ...]:
    """Freeze only compiler-created HTTP route extensions on the application."""

    normalized = tuple(values)
    if any(not isinstance(item, HTTPRouteExtension) for item in normalized):
        raise TypeError("http_routes must contain HTTPRouteExtension values")
    return normalized


def _telemetry_exporter_factories(
    values: Sequence[Any],
) -> tuple[LifecycleListener, ...]:
    """Freeze only discovered telemetry factories for runtime initialization."""

    normalized = tuple(values)
    if any(not isinstance(item, LifecycleListener) for item in normalized):
        raise TypeError("telemetry_exporters must contain LifecycleListener values")
    if any(item.phase != "telemetry_exporter" for item in normalized):
        raise TypeError("telemetry_exporters must contain telemetry_exporter factories")
    return normalized


def _context_capabilities(values: Sequence[Any]) -> tuple[ContextValue, ...]:
    """Freeze typed context resources and reject ambiguous lookup names."""

    normalized = tuple(values)
    if any(not isinstance(item, ContextValue) for item in normalized):
        raise TypeError("context_values must contain ContextValue values")
    names = [item.name for item in normalized]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError("duplicate context resource names: " + ", ".join(duplicates))
    return normalized


# RuntimeCapabilities remains an internal compiler/runtime seam; authored agents
# continue to depend on CompiledApplication's established flat attributes.
__all__ = ["CompiledApplication"]
