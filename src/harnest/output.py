"""Portable policies and models for agent output exposed to callers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal


SubagentMessageMode = Literal["suppress", "include"]
AgentMetadataMode = Literal["normalized", "raw"]


def _token_count(value: Any, field_name: str) -> int:
    """Require an exact non-negative count instead of accepting bools or floats."""

    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} cannot be negative")
    return value


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Provider-reported token counts normalized across ADK and LangGraph."""

    input_tokens: int
    output_tokens: int
    total_tokens: int

    def __post_init__(self) -> None:
        """Reject estimates and malformed provider values at the shared boundary."""

        _token_count(self.input_tokens, "input_tokens")
        _token_count(self.output_tokens, "output_tokens")
        _token_count(self.total_tokens, "total_tokens")

    def as_dict(self) -> dict[str, int]:
        """Return the camel-case representation used by public transports."""

        return {
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "totalTokens": self.total_tokens,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TokenUsage":
        """Parse either public camel-case or internal snake-case count names."""

        if not isinstance(value, Mapping):
            raise TypeError("token usage must be a mapping")
        return cls(
            input_tokens=value.get("inputTokens", value.get("input_tokens")),
            output_tokens=value.get("outputTokens", value.get("output_tokens")),
            total_tokens=value.get("totalTokens", value.get("total_tokens")),
        )


@dataclass(frozen=True, slots=True)
class AgentMetadata:
    """One framework-neutral view of model and provider response metadata.

    ``raw`` is populated only when :class:`OutputPolicy` explicitly selects
    ``agent_metadata="raw"``. The normalized fields remain available in both
    modes so consumers do not need framework-specific token or finish keys.
    """

    framework: str
    usage: TokenUsage | None = None
    model: str | None = None
    provider: str | None = None
    finish_reason: str | None = None
    raw: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        """Keep framework identity and optional normalized labels unambiguous."""

        if not isinstance(self.framework, str) or not self.framework:
            raise ValueError("agent metadata framework must be non-empty")
        for field_name in ("model", "provider", "finish_reason"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"agent metadata {field_name} must be non-empty")
        if self.raw is not None and not isinstance(self.raw, Mapping):
            raise TypeError("raw agent metadata must be a mapping")

    def as_dict(self) -> dict[str, Any]:
        """Return the stable public representation without empty optional fields."""

        value: dict[str, Any] = {"framework": self.framework}
        if self.usage is not None:
            value["usage"] = self.usage.as_dict()
        if self.model is not None:
            value["model"] = self.model
        if self.provider is not None:
            value["provider"] = self.provider
        if self.finish_reason is not None:
            value["finishReason"] = self.finish_reason
        if self.raw is not None:
            value["raw"] = dict(self.raw)
        return value

    def _as_runtime_event(self, *, agent: str | None = None) -> dict[str, Any]:
        """Build the internal snake-case event consumed by every transport."""

        event: dict[str, Any] = {
            "type": "agent_metadata",
            "framework": self.framework,
        }
        if agent:
            event["agent"] = agent
        if self.usage is not None:
            event["usage"] = {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "total_tokens": self.usage.total_tokens,
            }
        if self.model is not None:
            event["model"] = self.model
        if self.provider is not None:
            event["provider"] = self.provider
        if self.finish_reason is not None:
            event["finish_reason"] = self.finish_reason
        if self.raw is not None:
            event["raw"] = dict(self.raw)
            # Public projectors require this compiler-owned marker so an
            # unrelated lifecycle event cannot accidentally opt itself in.
            event["_raw_provider_metadata"] = True
        return event


def _reported_token_usage(
    input_tokens: Any,
    output_tokens: Any,
    total_tokens: Any = None,
) -> TokenUsage | None:
    """Normalize provider counts only when both input and output were reported."""

    input_count = _optional_token_count(input_tokens)
    output_count = _optional_token_count(output_tokens)
    if input_count is None or output_count is None:
        return None
    total_count = _optional_token_count(total_tokens)
    return TokenUsage(
        input_tokens=input_count,
        output_tokens=output_count,
        total_tokens=(
            total_count if total_count is not None else input_count + output_count
        ),
    )


def _optional_token_count(value: Any) -> int | None:
    """Ignore absent or malformed optional provider counters without estimating."""

    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


def _usage_from_runtime_event(event: Mapping[str, Any]) -> TokenUsage | None:
    """Read one normalized usage value from a portable metadata event."""

    if event.get("type") != "agent_metadata":
        return None
    usage = event.get("usage")
    if not isinstance(usage, Mapping):
        return None
    return _reported_token_usage(
        usage.get("input_tokens"),
        usage.get("output_tokens"),
        usage.get("total_tokens"),
    )


def _agent_metadata_from_runtime_event(event: Mapping[str, Any]) -> AgentMetadata:
    """Rebuild the shared model before projecting an internal metadata event."""

    framework = event.get("framework")
    if not isinstance(framework, str) or not framework:
        raise ValueError("agent_metadata events require a framework")
    raw = (
        event.get("raw")
        if event.get("_raw_provider_metadata") is True
        else None
    )
    return AgentMetadata(
        framework=framework,
        usage=_usage_from_runtime_event(event),
        model=_optional_text(event.get("model")),
        provider=_optional_text(event.get("provider")),
        finish_reason=_optional_text(event.get("finish_reason")),
        raw=dict(raw) if isinstance(raw, Mapping) else None,
    )


def _optional_text(value: Any) -> str | None:
    """Keep only non-empty textual values in normalized metadata fields."""

    return value if isinstance(value, str) and value else None


def _aggregate_token_usage(events: Sequence[Mapping[str, Any]]) -> TokenUsage | None:
    """Sum per-model-call usage without inventing counts for unreported calls."""

    values = [
        usage
        for event in events
        if (usage := _usage_from_runtime_event(event))
    ]
    if not values:
        return None
    return TokenUsage(
        input_tokens=sum(value.input_tokens for value in values),
        output_tokens=sum(value.output_tokens for value in values),
        total_tokens=sum(value.total_tokens for value in values),
    )


@dataclass(frozen=True, slots=True)
class OutputPolicy:
    """Control intermediate narration and provider metadata in public output."""

    subagent_messages: SubagentMessageMode = "suppress"
    agent_metadata: AgentMetadataMode = "normalized"
    persist_raw_agent_metadata: bool = False

    def __post_init__(self) -> None:
        """Reject misspelled policy values before the application can start."""

        if self.subagent_messages not in {"suppress", "include"}:
            raise ValueError(
                "subagent_messages must be either 'suppress' or 'include'"
            )
        if self.agent_metadata not in {"normalized", "raw"}:
            raise ValueError(
                "agent_metadata must be either 'normalized' or 'raw'"
            )
        if not isinstance(self.persist_raw_agent_metadata, bool):
            raise TypeError("persist_raw_agent_metadata must be a boolean")
        if self.persist_raw_agent_metadata and self.agent_metadata != "raw":
            raise ValueError(
                "persist_raw_agent_metadata requires agent_metadata='raw'"
            )

    def includes_intermediate_message(self, *, has_tool_calls: bool) -> bool:
        """Return whether narration attached to an intermediate tool call is public."""

        # Tool-free messages remain visible because a subagent may legitimately
        # own the final customer answer. Only pre-tool narration is policy-bound.
        return not has_tool_calls or self.subagent_messages == "include"


__all__ = [
    "AgentMetadata",
    "AgentMetadataMode",
    "OutputPolicy",
    "SubagentMessageMode",
    "TokenUsage",
]
