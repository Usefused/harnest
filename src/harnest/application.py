"""Framework boundary objects used by compiled Harnest artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .agent import _AdvancedAgentDefinition
from .checkpoint import ADKStore, CheckpointAuthority
from .context import ContextValue
from .output import OutputPolicy
from .session import SessionStore


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
    output_policy: OutputPolicy = OutputPolicy()
    checkpoint_metadata: dict[str, str] | None = None
    context_values: Sequence[ContextValue] = ()
    harnest_version: str | None = None
    framework_distribution: str | None = None
    framework_version: str | None = None

    def __post_init__(self) -> None:
        """Freeze normalized metadata at the compiled framework boundary."""

        if self.framework not in {"adk", "langgraph"}:
            raise ValueError(f"unsupported compiled framework: {self.framework}")
        if self.mode not in {"managed", "advanced"}:
            raise ValueError(f"unsupported compiled mode: {self.mode}")
        if self.kind not in {"agent", "graph", "advanced"}:
            raise ValueError(f"unsupported compiled application kind: {self.kind}")
        object.__setattr__(self, "extensions", tuple(self.extensions))
        object.__setattr__(
            self, "checkpoint_metadata", dict(self.checkpoint_metadata or {})
        )
        object.__setattr__(self, "context_values", tuple(self.context_values))


__all__ = ["CompiledApplication"]
