"""Structured contracts shared by helpdesk resources."""

from typing import Literal

from pydantic import BaseModel


class TriageResult(BaseModel):
    """Validated routing decision returned by the triage tool."""

    queue: str
    priority: Literal["low", "normal", "high", "urgent"]
    reason: str
