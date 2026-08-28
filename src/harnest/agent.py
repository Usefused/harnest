"""Framework-neutral managed agent definitions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence

from .mcp import MCPClient
from .mcp_lifecycle import propagate_mcp_lifecycles
from .model import ModelInput, resolve_model
from .model_lifecycle import propagate_litellm_lifecycles
from .sandbox import Sandbox
from .structured import (
    PydanticModel,
    framework_metadata_field,
    provider_output_schema,
    validate_output_schema,
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
    sandbox: Sandbox | Any | None = None
    input_schema: PydanticModel | None = None
    output_key: str | None = None
    output_schema: PydanticModel | None = None
    generate_content_config: Mapping[str, Any] | Any | None = None
    history: Literal["session", "turn"] = "session"

    @classmethod
    def advanced(
        cls,
        target: Any,
        *,
        name: str | None = None,
        input_adapter: Callable[[str, Mapping[str, Any]], Any] | None = None,
        output_adapter: Callable[[Any], Any] | None = None,
    ) -> "_AdvancedAgentDefinition":
        """Wrap a framework-native root, subagent, or graph node.

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
        self._validate_identity()
        self._validate_history()
        self._validate_resources()
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

    def build(self) -> Any:
        """Build the underlying ADK agent.

        ADK is imported lazily so config generation and validation do not need
        model/runtime dependencies loaded.
        """

        if self.instruction is None:
            raise ValueError(
                "agent instruction is unresolved; provide instruction or use "
                "bundle_agent(__file__, agent) with a sibling instructions.md"
            )

        try:
            from google.adk.agents import LlmAgent
        except ImportError as exc:  # pragma: no cover - depends on optional runtime
            raise RuntimeError(
                "google-adk is required to build an agent; install the harnest package"
            ) from exc

        kwargs = self._build_kwargs()
        built = LlmAgent(**kwargs)
        propagate_litellm_lifecycles(kwargs["model"], built)
        propagate_mcp_lifecycles(kwargs["tools"], built)
        for child in kwargs["sub_agents"]:
            propagate_litellm_lifecycles(child, built)
            propagate_mcp_lifecycles(child, built)
        return built

    def _build_kwargs(self) -> dict[str, Any]:
        """Translate the portable definition into ADK constructor arguments."""

        children = [_build_adk_subagent(child) for child in self.subagents]
        runtime_tools = [*self.tools, *(client.to_adk_toolset() for client in self.mcp)]
        kwargs: dict[str, Any] = {
            "name": self.name,
            "model": resolve_model(self.model),
            "instruction": self.instruction,
            "description": self.description,
            "tools": runtime_tools,
            "sub_agents": children,
            # Explicit modes keep graph-node behavior consistent with the
            # portable history contract instead of accepting ADK's node default.
            "mode": "chat" if self.history == "session" else "single_turn",
            "include_contents": "default"
            if self.history == "session"
            else "none",
        }
        if self.output_key is not None:
            kwargs["output_key"] = self.output_key
        kwargs.update(self._schema_kwargs())
        if self.sandbox is not None:
            kwargs["code_executor"] = (
                self.sandbox.to_adk_executor()
                if isinstance(self.sandbox, Sandbox)
                else self.sandbox
            )
        if self.generate_content_config is not None:
            kwargs["generate_content_config"] = self._content_config()
        return kwargs

    def _schema_kwargs(self) -> dict[str, PydanticModel]:
        """Expose only provider-owned fields to the native model boundary."""

        return {
            name: schema
            for name, schema in (
                ("input_schema", self.input_schema),
                (
                    "output_schema",
                    provider_output_schema(self.output_schema)
                    if self.output_schema is not None
                    else None,
                ),
            )
            if schema is not None
        }

    def _content_config(self) -> Any:
        """Translate authored mappings while preserving native ADK configuration."""

        config = self.generate_content_config
        if not isinstance(config, Mapping):
            return config
        try:
            from google.genai import types
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "google-genai is required for generate_content_config"
            ) from exc
        return types.GenerateContentConfig(**dict(config))


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


def _build_adk_subagent(value: Any) -> Any:
    """Build or unwrap one value for ADK's native subagent collection."""

    if isinstance(value, AgentDefinition):
        return value.build()
    if not isinstance(value, _AdvancedAgentDefinition):
        return value
    # Input and output adapters transform Harnest's root transport boundary;
    # applying them inside a parent would silently change the component contract.
    if value.input_adapter is not None or value.output_adapter is not None:
        raise ValueError(
            "embedded Agent.advanced subagents cannot use root input/output adapters"
        )
    try:
        from google.adk.agents import BaseAgent
        from google.adk.apps import App
    except ImportError as exc:  # pragma: no cover - optional runtime
        raise RuntimeError("advanced ADK subagents require google-adk") from exc
    # An App owns plugins and application services that cannot be preserved when
    # it is inserted into another application's delegation hierarchy.
    if isinstance(value.target, App):
        raise TypeError(
            "embedded Agent.advanced ADK subagents require a BaseAgent target; "
            "ADK App targets are root-only"
        )
    target = value.target
    if not isinstance(target, BaseAgent):
        raise TypeError(
            "embedded Agent.advanced ADK subagents require a BaseAgent target"
        )
    # ADK routes delegation by the native agent name. Accepting a different
    # wrapper identity would make discovery and runtime routing disagree.
    if value.name is not None and value.name != target.name:
        raise ValueError(
            "embedded Agent.advanced subagent name must match its native ADK "
            f"agent name {target.name!r}; got {value.name!r}"
        )
    return target


# A short alias reads naturally in agent.py: Agent(...).build().
Agent = AgentDefinition
