"""The small registry for framework-specific compiler behavior.

Filesystem discovery deliberately lives outside this package. Once discovery
has produced an :class:`AgentDefinition`, :class:`Graph`, or private advanced
agent definition,
the compiler selects a backend exactly once through :func:`get_backend`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from ..agent import AgentDefinition, _AdvancedAgentDefinition
from ..graph import Graph


class UnknownBackendError(ValueError):
    """The selected compiler backend is not registered."""


class BackendDependencyError(RuntimeError):
    """A selected backend's optional runtime dependencies are unavailable."""


class AdvancedBackendValidationError(TypeError):
    """An advanced-mode export is not executable by the selected backend."""


@dataclass(frozen=True, slots=True)
class AdvancedBackendResult:
    """Validated advanced target plus any provider application wrapper."""

    name: str
    target: Any
    native_app: Any | None = None


@dataclass(frozen=True, slots=True)
class Backend:
    """One compiler backend selected from the fixed Harnest registry."""

    name: str
    _lower_agent: Callable[[AgentDefinition, Sequence[Any], Any], Any]
    _lower_graph: Callable[[Graph, Sequence[Any], Any], Any]
    _wrap_managed: Callable[[Any, Sequence[Any]], Any | None]
    _validate_advanced: Callable[[_AdvancedAgentDefinition, str], AdvancedBackendResult]

    def lower_managed(
        self,
        value: AgentDefinition | Graph,
        *,
        native_extensions: Sequence[Any] = (),
        checkpointer: Any = None,
    ) -> Any:
        """Route one portable root through its selected backend ownership boundary."""

        if isinstance(value, AgentDefinition):
            return self._lower_agent(value, native_extensions, checkpointer)
        if isinstance(value, Graph):
            return self._lower_graph(value, native_extensions, checkpointer)
        raise TypeError("managed backend input must be AgentDefinition or Graph")

    def wrap_managed(
        self, target: Any, *, native_extensions: Sequence[Any] = ()
    ) -> Any | None:
        return self._wrap_managed(target, native_extensions)

    def validate_advanced(
        self, value: _AdvancedAgentDefinition, *, fallback_name: str
    ) -> AdvancedBackendResult:
        if not isinstance(value, _AdvancedAgentDefinition):
            raise TypeError("advanced backend input must come from Agent.advanced(...)")
        return self._validate_advanced(value, fallback_name)


def _lower_adk_agent(
    definition: AgentDefinition,
    _native_extensions: Sequence[Any],
    _checkpointer: Any,
) -> Any:
    return definition.build()


def _lower_adk_graph(
    graph: Graph, _native_extensions: Sequence[Any], _checkpointer: Any
) -> Any:
    from .adk import lower_graph

    return lower_graph(graph)


def _wrap_adk_managed(
    target: Any, native_extensions: Sequence[Any]
) -> Any | None:
    """Create the ADK App only after capabilities and plugins are complete."""

    # Tests and compiler extensions may replace AgentDefinition.build() with a
    # validation-only definition. Preserve that observable boundary without
    # pretending it is a runnable ADK application.
    if isinstance(target, AgentDefinition):
        return None
    try:
        from google.adk.apps import App
        from google.adk.apps.app import ResumabilityConfig
    except ImportError as exc:  # pragma: no cover - optional backend
        raise BackendDependencyError("ADK compilation requires google-adk") from exc
    from .._adk_warnings import suppress_adk_warnings

    # Resumability is required by Harnest's approval/checkpoint contract; its
    # experimental status is an implementation choice, not an authoring issue.
    with suppress_adk_warnings("resumability"):
        return App(
            name=target.name,
            root_agent=target,
            plugins=list(native_extensions),
            resumability_config=ResumabilityConfig(is_resumable=True),
        )


def _validate_adk_advanced(
    value: _AdvancedAgentDefinition, fallback_name: str
) -> AdvancedBackendResult:
    """Normalize supported native ADK roots without exposing NativeApp publicly."""

    try:
        from google.adk.agents import BaseAgent
        from google.adk.apps import App
        from google.adk.workflow import BaseNode
    except ImportError as exc:  # pragma: no cover - optional backend
        raise BackendDependencyError(
            "advanced ADK mode requires google-adk"
        ) from exc

    target = value.target
    native_app = None
    if isinstance(target, App):
        native_app = target
        target = target.root_agent
    elif isinstance(target, (BaseAgent, BaseNode)):
        name = value.name or getattr(target, "name", fallback_name)
        native_app = App(name=name, root_agent=target)
    else:
        raise AdvancedBackendValidationError(
            "advanced ADK mode expects google.adk App, BaseAgent, or BaseNode"
        )
    name = value.name or getattr(target, "name", None) or _fallback_name(fallback_name)
    return AdvancedBackendResult(name=name, target=target, native_app=native_app)


def _lower_langgraph_agent(
    definition: AgentDefinition,
    native_extensions: Sequence[Any],
    checkpointer: Any,
) -> Any:
    from .langgraph import lower_agent

    return lower_agent(
        definition, middleware=native_extensions, checkpointer=checkpointer
    )


def _lower_langgraph_graph(
    graph: Graph, native_extensions: Sequence[Any], checkpointer: Any
) -> Any:
    from .langgraph import lower_graph

    return lower_graph(graph, middleware=native_extensions, checkpointer=checkpointer)


def _wrap_langgraph_managed(
    target: Any, native_extensions: Sequence[Any]
) -> None:
    return None


def _validate_langgraph_advanced(
    value: _AdvancedAgentDefinition, fallback_name: str
) -> AdvancedBackendResult:
    """Accept only compiled Pregel targets at the advanced ownership boundary."""

    try:
        from langgraph.pregel import Pregel
    except ImportError as exc:  # pragma: no cover - optional backend
        raise BackendDependencyError(
            "advanced LangGraph mode requires langgraph"
        ) from exc

    target = value.target
    if not isinstance(target, Pregel):
        raise AdvancedBackendValidationError(
            "advanced LangGraph mode expects a compiled langgraph Pregel"
        )
    target.validate()
    name = value.name or getattr(target, "name", None) or _fallback_name(fallback_name)
    return AdvancedBackendResult(name=name, target=target)


def _fallback_name(value: str) -> str:
    return value.replace("-", "_")


_BACKENDS = {
    "adk": Backend(
        name="adk",
        _lower_agent=_lower_adk_agent,
        _lower_graph=_lower_adk_graph,
        _wrap_managed=_wrap_adk_managed,
        _validate_advanced=_validate_adk_advanced,
    ),
    "langgraph": Backend(
        name="langgraph",
        _lower_agent=_lower_langgraph_agent,
        _lower_graph=_lower_langgraph_graph,
        _wrap_managed=_wrap_langgraph_managed,
        _validate_advanced=_validate_langgraph_advanced,
    ),
}


def get_backend(name: str) -> Backend:
    """Return a registered compiler backend without importing its SDK yet."""

    try:
        return _BACKENDS[name]
    except (KeyError, TypeError) as exc:
        choices = " or ".join(_BACKENDS)
        raise UnknownBackendError(f"framework must be {choices}") from exc


def backend_names() -> tuple[str, ...]:
    """Return registered backend names in stable CLI order."""

    return tuple(_BACKENDS)


__all__ = [
    "Backend",
    "BackendDependencyError",
    "AdvancedBackendResult",
    "AdvancedBackendValidationError",
    "UnknownBackendError",
    "backend_names",
    "get_backend",
]
