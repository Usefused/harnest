"""Decorator-driven application lifecycle hooks.

Filesystem placement makes an extension module discoverable; these decorators
are the explicit opt-in that makes one of its functions executable by Harnest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, MutableMapping


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
    }
)
_FACTORY_PHASES = frozenset(
    {"adk_plugin", "langgraph_middleware", "session_store"}
)
_FRAMEWORKS = frozenset({"adk", "langgraph"})
_REGISTRATION_ATTRIBUTE = "__harnest_lifecycle_registration__"


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

    @property
    def identity(self) -> str:
        return f"{self.relative_path}:{self.line}:{self.function_name}"


@dataclass(slots=True)
class LifecycleContext:
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
class ModelLifecycleContext:
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


class _LifecycleDecorators:
    session_store = _PhaseDecorator("session_store")
    authenticate = _PhaseDecorator("authenticate")
    before_invoke = _PhaseDecorator("before_invoke")
    after_invoke = _PhaseDecorator("after_invoke")
    on_event = _PhaseDecorator("on_event")
    on_error = _PhaseDecorator("on_error")
    before_model = _PhaseDecorator("before_model")
    after_model = _PhaseDecorator("after_model")
    on_model_error = _PhaseDecorator("on_model_error")

    def adk_plugin(
        self, function: Callable[..., Any] | None = None, *, order: int = 0
    ) -> Any:
        decorator = _registration_decorator(
            "adk_plugin", order=order, framework="adk"
        )
        return decorator if function is None else decorator(function)

    def langgraph_middleware(
        self, function: Callable[..., Any] | None = None, *, order: int = 0
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        decorator = _registration_decorator(
            "langgraph_middleware", order=order, framework="langgraph"
        )
        return decorator if function is None else decorator(function)


def _registration_decorator(
    phase: str, *, order: int, framework: str | None = None
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    if phase not in _PHASES | _FACTORY_PHASES:
        raise ValueError(f"unsupported lifecycle phase {phase!r}")
    if not isinstance(order, int) or isinstance(order, bool):
        raise TypeError("lifecycle order must be an integer")

    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        if not callable(function):
            raise TypeError("lifecycle decorators require a callable")
        if hasattr(function, _REGISTRATION_ATTRIBUTE):
            raise TypeError("a function may have only one lifecycle decorator")
        setattr(
            function,
            _REGISTRATION_ATTRIBUTE,
            LifecycleRegistration(phase, order, framework),
        )
        return function

    return decorate


def registration_for(function: Any) -> LifecycleRegistration | None:
    """Return decorator metadata without treating imported aliases as hooks."""

    value = getattr(function, _REGISTRATION_ATTRIBUTE, None)
    return value if isinstance(value, LifecycleRegistration) else None


lifecycle = _LifecycleDecorators()


__all__ = [
    "DROP_EVENT",
    "LifecycleContext",
    "LifecycleListener",
    "LifecycleRegistration",
    "ModelCallRequest",
    "ModelCallResponse",
    "ModelLifecycleContext",
    "ModelMessage",
    "lifecycle",
]
