"""Framework-neutral managed agent definitions."""

from __future__ import annotations

import functools
import inspect
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Literal, Mapping, Sequence

from pydantic import BaseModel

from .mcp import MCPClient
from .mcp_lifecycle import propagate_mcp_lifecycles
from .model import ModelInput, resolve_model
from .model_lifecycle import propagate_litellm_lifecycles
from .durable import adk_durable_tool, is_durable_tool
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
    input_schema: PydanticModel | None = None
    output_key: str | None = None
    output_schema: PydanticModel | None = None
    generate_content_config: Mapping[str, Any] | Any | None = None
    history: Literal["session", "turn"] = "session"
    sandboxes: Sequence[str] = field(default_factory=tuple)
    _sandbox_bindings: Mapping[str, Sandbox] = field(default_factory=dict, repr=False)

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
        """Validate declarations and snapshot sandbox grants before lowering."""
        self._validate_identity()
        self._validate_history()
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
        from .sandbox_assignments import validate_sandbox_name

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

    def build(self) -> Any:
        """Build the underlying ADK agent.

        ADK is imported lazily so config generation and validation do not need
        model/runtime dependencies loaded. Sandbox agents retain native flows
        with a scoped consumed-code continuation compatibility adapter.
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
        from .agent_scope_adk import managed_adk_agent_type

        native_type = managed_adk_agent_type(LlmAgent)
        built = native_type(**kwargs)
        propagate_litellm_lifecycles(kwargs["model"], built)
        propagate_mcp_lifecycles(kwargs["tools"], built)
        for child in kwargs["sub_agents"]:
            propagate_litellm_lifecycles(child, built)
            propagate_mcp_lifecycles(child, built)
        return built

    def _build_kwargs(self) -> dict[str, Any]:
        """Translate the portable definition into ADK constructor arguments."""

        children = [_build_adk_subagent(child) for child in self.subagents]
        runtime_tools = [
            *(_adk_runtime_tool(tool) for tool in self.tools),
            *(client.to_adk_toolset() for client in self.mcp),
        ]
        from .sandbox_assignments import assigned_sandboxes

        # Named declarations are capabilities for authored tools, never an
        # implicit model-facing execute-code tool or native code executor.
        assigned_sandboxes(self)
        kwargs: dict[str, Any] = {
            "name": self.name,
            "model": resolve_model(self.model),
            "instruction": self.instruction,
            "description": self.description,
            "tools": runtime_tools,
            "sub_agents": children,
            # ADK rejects single_turn at an application root. Excluding prior
            # contents preserves the portable turn-only contract there; the
            # graph backend selects single_turn when this agent is a node.
            "mode": "chat",
            "include_contents": "default"
            if self.history == "session"
            else "none",
        }
        if self.output_key is not None:
            kwargs["output_key"] = self.output_key
        kwargs.update(self._schema_kwargs())
        if self.generate_content_config is not None:
            kwargs["generate_content_config"] = self._content_config()
        return kwargs

    def _schema_kwargs(self) -> dict[str, PydanticModel]:
        """Expose only provider-owned output fields to the ADK node boundary.

        Harnest validates portable request models before framework lowering.
        Passing that schema to ADK would make ADK concatenate multipart content
        into text and reject otherwise-valid media references before its model
        callback can materialize them.
        """

        return {
            name: schema
            for name, schema in (
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


def _adk_runtime_tool(tool: Tool) -> Tool:
    """Serialize validated Pydantic tool values before ADK wraps responses."""

    if not callable(tool):
        return tool
    runtime_tool = _adk_serializing_tool(tool)
    return adk_durable_tool(runtime_tool) if is_durable_tool(tool) else runtime_tool


def _adk_serializing_tool(tool: Tool) -> Tool:
    """Build the value adapter that executes inside any durable ADK boundary."""

    if getattr(tool, "__harnest_output_schema__", None) is None:
        return tool
    if inspect.iscoroutinefunction(tool):

        @functools.wraps(tool)
        async def async_call(*args: Any, **kwargs: Any) -> Any:
            return _adk_tool_result(await tool(*args, **kwargs))

        return async_call

    @functools.wraps(tool)
    def sync_call(*args: Any, **kwargs: Any) -> Any:
        return _adk_tool_result(tool(*args, **kwargs))

    return sync_call


def _adk_tool_result(value: Any) -> Any:
    """Avoid ADK's generic ``result`` envelope for Pydantic model values."""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    return value


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
    from .agent_scope_adk import scope_native_adk_agent

    if not isinstance(value, _AdvancedAgentDefinition):
        return scope_native_adk_agent(value)
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
    return scope_native_adk_agent(target)


# A short alias reads naturally in agent.py: Agent(...).build().
Agent = AgentDefinition
