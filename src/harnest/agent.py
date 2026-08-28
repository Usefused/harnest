"""Small, typed conveniences around Google ADK agent definitions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .mcp import MCPClient
from .model import ModelInput, resolve_model
from .model_lifecycle import propagate_litellm_lifecycles
from .sandbox import Sandbox

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
    """Framework-neutral definition that builds a Google ADK ``LlmAgent``."""

    name: str
    model: ModelInput
    instruction: str | None = None
    description: str = ""
    tools: Sequence[Tool] = field(default_factory=tuple)
    subagents: Sequence["AgentDefinition | Any"] = field(default_factory=tuple)
    mcp: Sequence[MCPClient] = field(default_factory=tuple)
    sandbox: Sandbox | Any | None = None
    output_key: str | None = None
    generate_content_config: Mapping[str, Any] | Any | None = None

    @classmethod
    def advanced(
        cls,
        target: Any,
        *,
        name: str | None = None,
        input_adapter: Callable[[str, Mapping[str, Any]], Any] | None = None,
        output_adapter: Callable[[Any], Any] | None = None,
    ) -> "_AdvancedAgentDefinition":
        """Wrap a framework-native application for advanced compilation.

        Managed agents continue to use ``Agent(...)``. This constructor is the
        explicit escape hatch for applications authored directly with ADK or
        LangGraph.
        """

        return _AdvancedAgentDefinition(
            target=target,
            name=name,
            input_adapter=input_adapter,
            output_adapter=output_adapter,
        )

    def __post_init__(self) -> None:
        self._validate_identity()
        self._validate_resources()

    def _validate_identity(self) -> None:
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
        for child in kwargs["sub_agents"]:
            propagate_litellm_lifecycles(child, built)
        return built

    def _build_kwargs(self) -> dict[str, Any]:
        children = [
            child.build() if isinstance(child, AgentDefinition) else child
            for child in self.subagents
        ]
        runtime_tools = [*self.tools, *(client.to_adk_toolset() for client in self.mcp)]
        kwargs: dict[str, Any] = {
            "name": self.name,
            "model": resolve_model(self.model),
            "instruction": self.instruction,
            "description": self.description,
            "tools": runtime_tools,
            "sub_agents": children,
        }
        if self.output_key is not None:
            kwargs["output_key"] = self.output_key
        if self.sandbox is not None:
            kwargs["code_executor"] = (
                self.sandbox.to_adk_executor()
                if isinstance(self.sandbox, Sandbox)
                else self.sandbox
            )
        if self.generate_content_config is not None:
            kwargs["generate_content_config"] = self._content_config()
        return kwargs

    def _content_config(self) -> Any:
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


# A short alias reads naturally in agent.py: Agent(...).build().
Agent = AgentDefinition
