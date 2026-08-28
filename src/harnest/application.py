"""Framework boundary objects used by compiled Harnest artifacts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .agent import _AdvancedAgentDefinition
from .checkpoint import ADKStore, CheckpointAuthority
from .context import ContextValue
from .credentials import CredentialProvider
from .output import OutputPolicy
from .session import SessionStore
from .structured import PydanticModel, validate_output_schema


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
    credential_provider: CredentialProvider | None = field(default=None, repr=False)
    output_policy: OutputPolicy = OutputPolicy()
    telemetry_exporters: Sequence[Any] = field(default=(), repr=False)
    input_schema: PydanticModel | None = None
    output_schema: PydanticModel | None = None
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
        if self.credential_provider is not None and not isinstance(
            self.credential_provider, CredentialProvider
        ):
            raise TypeError("credential_provider must implement CredentialProvider")
        object.__setattr__(self, "extensions", tuple(self.extensions))
        object.__setattr__(
            self, "telemetry_exporters", tuple(self.telemetry_exporters)
        )
        object.__setattr__(
            self, "checkpoint_metadata", dict(self.checkpoint_metadata or {})
        )
        object.__setattr__(self, "context_values", tuple(self.context_values))
        validate_output_schema(
            self.input_schema, field_name="compiled application input_schema"
        )
        validate_output_schema(
            self.output_schema, field_name="compiled application output_schema"
        )


__all__ = ["CompiledApplication"]
