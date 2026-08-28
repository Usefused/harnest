"""Portable policy for selecting model messages exposed to callers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


SubagentMessageMode = Literal["suppress", "include"]


@dataclass(frozen=True, slots=True)
class OutputPolicy:
    """Control whether intermediate subagent narration becomes public output."""

    subagent_messages: SubagentMessageMode = "suppress"

    def __post_init__(self) -> None:
        """Reject misspelled policy values before the application can start."""

        if self.subagent_messages not in {"suppress", "include"}:
            raise ValueError(
                "subagent_messages must be either 'suppress' or 'include'"
            )

    def includes_intermediate_message(self, *, has_tool_calls: bool) -> bool:
        """Return whether narration attached to an intermediate tool call is public."""

        # Tool-free messages remain visible because a subagent may legitimately
        # own the final customer answer. Only pre-tool narration is policy-bound.
        return not has_tool_calls or self.subagent_messages == "include"


__all__ = ["OutputPolicy", "SubagentMessageMode"]
