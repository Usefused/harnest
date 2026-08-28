"""Public result and framework metadata contracts for the demo agent."""

from typing import Any

from pydantic import BaseModel, Field

from harnest import FrameworkMetadata


class ADKTurnMetadata(BaseModel):
    """ADK events retained for the completed turn."""

    events: list[dict[str, Any]]


class LangGraphTurnMetadata(BaseModel):
    """LangGraph messages retained for the completed turn."""

    messages: list[dict[str, Any]] = Field(default_factory=list)


class TurnMetadata(BaseModel):
    """One native framework namespace populated by the managed runtime."""

    adk: ADKTurnMetadata | None = None
    langgraph: LangGraphTurnMetadata | None = None


class MetadataResult(BaseModel):
    """Portable answer with explicitly requested runtime-owned metadata."""

    answer: str
    metadata: FrameworkMetadata[TurnMetadata]
