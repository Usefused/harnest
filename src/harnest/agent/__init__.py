"""Framework-neutral managed agent definitions and tool contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping, Sequence

from ..agent_principal import AgentRuntimePermissionError, AgentRuntimePrincipal
from .._agent_tool import tool
from ..client_tool import (
    ClientToolError,
    ClientToolExecution,
    InMemoryClientToolStore,
    PendingClientTool,
    client_tool,
    client_tool_execution,
    current_transient_media,
)
from ..client_input import client_input
# MCP imports approval from this package; annotations must not make the domain
# modules depend on which public import a developer happens to write first.
if TYPE_CHECKING:
    from ..mcp import MCPClient
from ..model import ModelInput
from ..tokens import TokenPolicy
from ..skill_selection import DecisionSkillSelector
from ..sandbox import Sandbox
from ..structured import (
    PydanticModel,
    framework_metadata_field,
    validate_output_schema,
)
from ..tool_lifecycle import (
    ToolCallRequest,
    ToolLifecycleContext,
    ToolLifecycleError,
)

Tool = Callable[..., Any] | Any

_AGENT_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def instruction_file(anchor: str | Path, relative_path: str = "instructions.md") -> str:
    """Read an instruction file relative to a Python module.

    Pass ``__file__`` as ``anchor`` so the path remains correct in a deployed
    bundle and does not depend on the process working directory.
    """

    anchor_path = Path(anchor).resolve()
    path = (
        anchor_path.parent / relative_path
        if anchor_path.is_file()
        else anchor_path / relative_path
    )
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"unable to read agent instructions at {path}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """Managed agent definition lowered by the selected Harnest backend."""

    name: str
    model: ModelInput
    instruction: str | None = None
    description: str = ""
    tools: Sequence[Tool] = field(default_factory=tuple)
    subagents: Sequence["AgentDefinition | _AdvancedAgentDefinition | Any"] = field(
        default_factory=tuple
    )
    mcp: Sequence[MCPClient] = field(default_factory=tuple)
    input_schema: PydanticModel | None = None
    output_key: str | None = None
    output_schema: PydanticModel | None = None
    generate_content_config: Mapping[str, Any] | Any | None = None
    history: Literal["session", "turn"] = "session"
    token_policy: TokenPolicy | None = field(default=None, kw_only=True)
    skill_selection: DecisionSkillSelector | None = field(default=None, kw_only=True)
    sandboxes: Sequence[str] = field(default_factory=tuple)
    _sandbox_bindings: Mapping[str, Sandbox] = field(default_factory=dict, repr=False)

    def build(self) -> Any:
        """Build this definition using the framework selected by Harnest."""

        from ..frameworks.context import current_framework

        return current_framework().lower_agent(
            self, framework_extensions=(), checkpointer=None
        )

    @classmethod
    def advanced(
        cls,
        target: Any,
        *,
        name: str | None = None,
        input_adapter: Callable[[str, Mapping[str, Any]], Any] | None = None,
        output_adapter: Callable[[Any], Any] | None = None,
    ) -> "_AdvancedAgentDefinition":
        """Wrap a framework-specific root, subagent, or graph node.

        Export the wrapper as the root in advanced mode, or embed it in a
        managed agent/graph when only one component needs native behavior.
        """

        return _AdvancedAgentDefinition(
            target=target,
            name=name,
            input_adapter=input_adapter,
            output_adapter=output_adapter,
        )

    def __post_init__(self) -> None:
        """Validate declarations and snapshot sandbox grants before lowering."""
        self._validate_identity()
        self._validate_history()
        if self.token_policy is not None and not isinstance(self.token_policy, TokenPolicy):
            raise TypeError("token_policy must be TokenPolicy or None")
        if self.skill_selection is not None and not isinstance(self.skill_selection, DecisionSkillSelector):
            raise TypeError("skill_selection must be DecisionSkillSelector or None")
        self._validate_resources()
        self._validate_sandboxes()
        validate_output_schema(self.input_schema, field_name="agent input_schema")
        validate_output_schema(self.output_schema, field_name="agent output_schema")
        if self.output_schema is not None:
            framework_metadata_field(self.output_schema)

    def _validate_identity(self) -> None:
        """Keep authored identity safe across framework and artifact boundaries."""

        if not isinstance(self.name, str) or not _AGENT_NAME_PATTERN.fullmatch(self.name):
            raise ValueError(
                "agent name must start with a letter or underscore and contain "
                "only ASCII letters, numbers, and underscores"
            )
        if self.model is None or (
            isinstance(self.model, str) and not self.model.strip()
        ):
            raise ValueError("agent model is required")
        if self.instruction is not None and (
            not isinstance(self.instruction, str) or not self.instruction.strip()
        ):
            raise ValueError("agent instruction is required")
    def _validate_history(self) -> None:
        if not isinstance(self.history, str) or self.history not in {
            "session",
            "turn",
        }:
            raise ValueError("agent history must be 'session' or 'turn'")

    def _validate_resources(self) -> None:
        for field_name, value in (
            ("tools", self.tools),
            ("subagents", self.subagents),
            ("mcp", self.mcp),
        ):
            if isinstance(value, (str, bytes)):
                raise TypeError(f"agent {field_name} must be a sequence, not a string")

    def _validate_sandboxes(self) -> None:
        """Keep named grants immutable and separate from the legacy executor path."""
        from ..sandbox_assignments import validate_sandbox_name

        if isinstance(self.sandboxes, (str, bytes)) or not isinstance(self.sandboxes, Sequence):
            raise TypeError("agent sandboxes must be a sequence of names, such as ['research']")
        names = tuple(self.sandboxes)
        for name in names:
            validate_sandbox_name(name)
        if len(names) != len(set(names)):
            raise ValueError("agent sandboxes must not contain duplicate names")
        if not isinstance(self._sandbox_bindings, Mapping):
            raise TypeError("resolved sandbox bindings must be a mapping")
        if any(not isinstance(value, Sandbox) for value in self._sandbox_bindings.values()):
            raise TypeError("resolved sandbox bindings must contain Sandbox definitions")
        object.__setattr__(self, "sandboxes", names)
        object.__setattr__(self, "_sandbox_bindings", MappingProxyType(dict(self._sandbox_bindings)))


@dataclass(frozen=True, slots=True)
class _AdvancedAgentDefinition:
    """Private compiler boundary produced only by :meth:`Agent.advanced`."""

    target: Any
    name: str | None = None
    input_adapter: Callable[[str, Mapping[str, Any]], Any] | None = None
    output_adapter: Callable[[Any], Any] | None = None

    def __post_init__(self) -> None:
        if self.target is None:
            raise ValueError("Agent.advanced target is required")
        if self.name is not None and (
            not isinstance(self.name, str) or not self.name.strip()
        ):
            raise ValueError("Agent.advanced name must be a non-empty string")


# A short alias reads naturally in agent.py: Agent(...).
Agent = AgentDefinition

# Approval is an agent-owned capability namespace rather than a root domain.
from . import approval as approval


__all__ = [
    "Agent",
    "AgentDefinition",
    "AgentRuntimePermissionError",
    "AgentRuntimePrincipal",
    "ClientToolError",
    "ClientToolExecution",
    "InMemoryClientToolStore",
    "PendingClientTool",
    "ToolCallRequest",
    "ToolLifecycleContext",
    "ToolLifecycleError",
    "client_input",
    "client_tool",
    "client_tool_execution",
    "current_transient_media",
    "instruction_file",
    "approval",
    "tool",
]
