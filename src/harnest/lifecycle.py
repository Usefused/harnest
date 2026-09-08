"""Decorator-driven application lifecycle hooks.

Filesystem placement makes an extension module discoverable; these decorators
are the explicit opt-in that makes one of its functions executable by Harnest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Callable, Mapping, MutableMapping

from .lifecycle_transition import Finish, Next, TransitionContext
from .lifecycle_coverage import CoverageLevel, LifecycleCoverage, lifecycle_coverage


_PHASES = frozenset(
    {
        "authenticate",
        "before_invoke",
        "after_invoke",
        "on_event",
        "on_error",
        "before_model",
        "after_model",
        "on_model_error",
        "before_tool",
        "after_tool",
        "on_tool_error",
        "before_http",
        "after_http",
        "on_http_error",
        "before_mcp",
        "after_mcp",
        "on_mcp_error",
    }
)
_FACTORY_PHASES = frozenset(
    {
        "adk_plugin",
        "langgraph_middleware",
        "session_store",
        "checkpointer",
        "asset_store",
        "credential_provider",
        "http_routes",
        "output_policy",
        "telemetry_exporter",
        "resource",
        "custom_store",
        "task_store",
        "cron_store",
        "skill_source",
    }
)
_FRAMEWORKS = frozenset({"adk", "langgraph"})
_REGISTRATION_ATTRIBUTE = "__harnest_lifecycle_registration__"
_STORAGE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._~-]{0,63}$")
_STORAGE_PHASES = frozenset(
    {"session_store", "checkpointer", "asset_store", "custom_store", "task_store", "cron_store"}
)
_DECORATOR_PATHS = {
    "session_store": "storage.sessions",
    "checkpointer": "storage.checkpoints",
    "asset_store": "storage.assets",
    "task_store": "storage.tasks",
    "cron_store": "storage.cron",
    "before_invoke": "agent.before",
    "after_invoke": "agent.after",
    "on_event": "agent.on_event",
    "on_error": "agent.on_error",
    "before_model": "model.before",
    "after_model": "model.after",
    "on_model_error": "model.on_error",
    "before_tool": "tool.before",
    "after_tool": "tool.after",
    "on_tool_error": "tool.on_error",
    "before_http": "http.before",
    "after_http": "http.after",
    "on_http_error": "http.on_error",
    "before_mcp": "mcp.before",
    "after_mcp": "mcp.after",
    "on_mcp_error": "mcp.on_error",
}


class _DropEvent:
    def __repr__(self) -> str:
        return "DROP_EVENT"


DROP_EVENT = _DropEvent()
"""Return from an ``on_event`` listener to omit the current event."""


@dataclass(frozen=True, slots=True)
class LifecycleRegistration:
    """Metadata attached by one lifecycle decorator."""

    phase: str
    order: int
    framework: str | None = None
    name: str | None = None


@dataclass(frozen=True, slots=True)
class LifecycleListener:
    """One discovered callback with a deterministic source identity."""

    phase: str
    callback: Callable[..., Any]
    order: int
    relative_path: str
    line: int
    function_name: str
    framework: str | None = None
    context_name: str | None = None
    registration_name: str | None = None

    @property
    def identity(self) -> str:
        """Return the stable identity used to order and deduplicate listeners."""

        return f"{self.relative_path}:{self.line}:{self.function_name}"


@dataclass(slots=True)
class LifecycleContext(TransitionContext):
    """Portable invocation context shared by listeners for one execution."""

    framework: str
    agent_name: str
    invocation_id: str
    user_id: str
    session_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    attributes: MutableMapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.framework not in _FRAMEWORKS:
            raise ValueError(f"unsupported lifecycle framework {self.framework!r}")
        for name in ("agent_name", "invocation_id", "user_id", "session_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        if not isinstance(self.attributes, MutableMapping):
            raise TypeError("attributes must be a mutable mapping")


@dataclass(frozen=True, slots=True)
class ModelMessage:
    """One provider-independent text message in a model call."""

    role: str
    text: str


@dataclass(frozen=True, slots=True)
class ModelCallRequest:
    """Portable model input that a listener may replace or short-circuit."""

    model: str | None
    messages: tuple[ModelMessage, ...]


@dataclass(frozen=True, slots=True)
class ModelCallResponse:
    """Portable visible model output."""

    text: str


@dataclass(frozen=True, slots=True)
class ModelLifecycleContext(TransitionContext):
    """Stable provider-neutral facts associated with one model call."""

    framework: str
    agent_name: str | None = None
    invocation_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None

    def __post_init__(self) -> None:
        if self.framework not in _FRAMEWORKS:
            raise ValueError(f"unsupported model lifecycle framework {self.framework!r}")


class _PhaseDecorator:
    def __init__(self, phase: str) -> None:
        self._phase = phase

    def __call__(
        self, function: Callable[..., Any] | None = None, *, order: int = 0
    ) -> Any:
        decorator = _registration_decorator(self._phase, order=order)
        return decorator if function is None else decorator(function)


class _StorageDecorators:
    """Namespace storage roles while retaining distributed file discovery."""

    sessions = _PhaseDecorator("session_store")
    checkpoints = _PhaseDecorator("checkpointer")
    tasks = _PhaseDecorator("task_store")
    cron = _PhaseDecorator("cron_store")

    def assets(self, name: str, *, order: int = 0) -> Any:
        """Declare one named asset authority assembled into the registry."""

        _validate_storage_name(name, kind="asset store")
        return _registration_decorator("asset_store", order=order, name=name)

    def custom(self, name: str, *, order: int = 0) -> Any:
        """Declare lifecycle-owned application storage under an explicit name."""

        _validate_storage_name(name, kind="custom storage")
        return _registration_decorator("custom_store", order=order, name=name)


class _ToolDecorators:
    """Group portable tool interception under its first-class domain path."""

    before = _PhaseDecorator("before_tool")
    after = _PhaseDecorator("after_tool")
    on_error = _PhaseDecorator("on_tool_error")


class _ModelDecorators:
    """Group provider-neutral model hooks under one authoring namespace."""

    before = _PhaseDecorator("before_model")
    after = _PhaseDecorator("after_model")
    on_error = _PhaseDecorator("on_model_error")


class _AgentDecorators:
    """Name invocation hooks by their agent lifecycle responsibility."""

    before = _PhaseDecorator("before_invoke")
    after = _PhaseDecorator("after_invoke")
    on_event = _PhaseDecorator("on_event")
    on_error = _PhaseDecorator("on_error")


class _HTTPDecorators:
    """Group server middleware hooks under the HTTP lifecycle boundary."""

    before = _PhaseDecorator("before_http")
    after = _PhaseDecorator("after_http")
    on_error = _PhaseDecorator("on_http_error")


class _MCPDecorators:
    """Group managed remote-tool hooks separately from transport ownership."""

    before = _PhaseDecorator("before_mcp")
    after = _PhaseDecorator("after_mcp")
    on_error = _PhaseDecorator("on_mcp_error")


class _SkillDecorators:
    """Register named runtime catalogs independently of filesystem skills."""

    def source(self, name: str, *, order: int = 0) -> Any:
        """Declare one named provider queried only during managed execution."""

        _validate_storage_name(name, kind="skill source", identifier="source")
        return _registration_decorator("skill_source", order=order, name=name)


# Public lifecycle authoring is the module namespace itself. Keeping the
# decorator families here prevents ``harnest.lifecycle.lifecycle`` from becoming
# a second, import-style-dependent public surface.
coverage = lifecycle_coverage
storage = _StorageDecorators()
tool = _ToolDecorators()
model = _ModelDecorators()
agent = _AgentDecorators()
http = _HTTPDecorators()
mcp = _MCPDecorators()
skills = _SkillDecorators()

credential_provider = _PhaseDecorator("credential_provider")
http_routes = _PhaseDecorator("http_routes")
output_policy = _PhaseDecorator("output_policy")
telemetry_exporter = _PhaseDecorator("telemetry_exporter")
resource = _PhaseDecorator("resource")
authenticate = _PhaseDecorator("authenticate")


def adk_plugin(
    function: Callable[..., Any] | None = None, *, order: int = 0
) -> Any:
    """Register a native ADK plugin factory at the lifecycle boundary."""

    decorator = _registration_decorator(
        "adk_plugin", order=order, framework="adk"
    )
    return decorator if function is None else decorator(function)


def langgraph_middleware(
    function: Callable[..., Any] | None = None, *, order: int = 0
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a native LangGraph middleware factory at the lifecycle boundary."""

    decorator = _registration_decorator(
        "langgraph_middleware", order=order, framework="langgraph"
    )
    return decorator if function is None else decorator(function)


def _registration_decorator(
    phase: str,
    *,
    order: int,
    framework: str | None = None,
    name: str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    if phase not in _PHASES | _FACTORY_PHASES:
        raise ValueError(f"unsupported lifecycle phase {phase!r}")
    if not isinstance(order, int) or isinstance(order, bool):
        raise TypeError("lifecycle order must be an integer")

    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        if not callable(function):
            raise TypeError("lifecycle decorators require a callable")
        if getattr(function, "__harnest_tool__", False):
            raise TypeError("tools cannot also be lifecycle extensions")
        existing = registrations_for(function)
        if existing and not _can_stack_storage(existing, phase):
            raise TypeError("a function may have only one lifecycle decorator")
        registration = LifecycleRegistration(phase, order, framework, name)
        if registration in existing:
            raise TypeError("a storage role may be declared only once per factory")
        setattr(
            function,
            _REGISTRATION_ATTRIBUTE,
            existing + (registration,),
        )
        return function

    return decorate


def registration_for(function: Any) -> LifecycleRegistration | None:
    """Return decorator metadata without treating imported aliases as hooks."""

    registrations = registrations_for(function)
    return registrations[0] if registrations else None


def registrations_for(function: Any) -> tuple[LifecycleRegistration, ...]:
    """Return every role attached to a possibly shared storage factory."""

    value = getattr(function, _REGISTRATION_ATTRIBUTE, ())
    if isinstance(value, LifecycleRegistration):
        return (value,)
    if isinstance(value, tuple) and all(
        isinstance(item, LifecycleRegistration) for item in value
    ):
        return value
    return ()


def _can_stack_storage(
    existing: tuple[LifecycleRegistration, ...], phase: str
) -> bool:
    """Allow one factory to fulfil storage roles without enabling ambiguous hooks."""

    return phase in _STORAGE_PHASES and all(
        item.phase in _STORAGE_PHASES for item in existing
    )


def _validate_storage_name(
    name: str, *, kind: str, identifier: str = "storage"
) -> None:
    """Keep authored names portable across references and serialized artifacts."""

    if not isinstance(name, str) or not _STORAGE_NAME.fullmatch(name):
        raise ValueError(f"{kind} name must be a valid {identifier} identifier")


def _decorator_path_for_phase(phase: str) -> str:
    """Return the sole public decorator path for an internal lifecycle phase."""

    return _DECORATOR_PATHS.get(phase, phase)


__all__ = [
    "CoverageLevel", "LifecycleCoverage", "lifecycle_coverage", "TransitionContext",
    "DROP_EVENT",
    "Finish",
    "LifecycleContext",
    "LifecycleListener",
    "LifecycleRegistration",
    "ModelCallRequest",
    "ModelCallResponse",
    "ModelLifecycleContext",
    "ModelMessage",
    "Next",
    "adk_plugin",
    "agent",
    "authenticate",
    "coverage",
    "credential_provider",
    "http",
    "http_routes",
    "langgraph_middleware",
    "mcp",
    "model",
    "output_policy",
    "registrations_for",
    "resource",
    "skills",
    "storage",
    "telemetry_exporter",
    "tool",
]
