"""Validated model budgets shared by Studio and its compiled assistant."""

import os

from pydantic import BaseModel, ConfigDict, Field


class AssistantLimits(BaseModel):
    """Bound each provider call and leave time for runtime and transport cleanup."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    timeout: int = Field(default=120, ge=1, le=1800)
    max_tokens: int = Field(default=12000, ge=1, le=131072)

    @classmethod
    def from_environment(cls):
        """Reject malformed host configuration before starting a model process."""
        return cls(timeout=int(os.getenv("HARNEST_BUILDER_TIMEOUT_SECONDS", "120")),
                   max_tokens=int(os.getenv("HARNEST_BUILDER_MAX_TOKENS", "12000")))

    def environment(self) -> dict[str, str]:
        """Keep outer deadlines longer than the selected provider deadline."""
        return {"HARNEST_BUILDER_TIMEOUT_SECONDS": str(self.timeout),
                "HARNEST_BUILDER_MAX_TOKENS": str(self.max_tokens),
                "HARNEST_BUILDER_REQUEST_TIMEOUT_SECONDS": str(self.timeout + 60)}
