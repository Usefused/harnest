"""The example's internal recommendation and public reply, without echoing ticket contents."""

from typing import Literal

from pydantic import BaseModel, Field


class TriageResult(BaseModel):
    """Distinguish a model recommendation from an offline fixture or provider failure."""

    action: Literal["route", "review"]
    queue: Literal["billing", "support"] | None
    department: Literal["billing", "support", "other"] | None
    confidence: float | None = Field(ge=0, le=1)
    reason: Literal["classified", "low_confidence", "other", "evaluation_failed"]
    mode: Literal["live", "offline"]
    provider: str
    provider_version: str
    error: str | None


class SupportResponse(BaseModel):
    """Expose only the reply; OutputPolicy separately controls decision results."""

    reply: str = Field(min_length=1)
