"""Real-model ADK consumer used only by the fully gated live journey."""

from __future__ import annotations

import os

from harnest.agent import Agent
from harnest.model import LiteLLMModel


def _required_model() -> str:
    """Require explicit live-model selection without inspecting credentials."""

    value = os.environ.get("LITELLM_MODEL")
    if not value or not value.strip():
        raise RuntimeError("LITELLM_MODEL is required for the real-model fixture")
    return value.strip()


def _model() -> LiteLLMModel:
    """Build the configured provider model with deterministic tool selection."""

    arguments: dict[str, object] = {"temperature": 0}
    api_base = os.environ.get("LITELLM_API_BASE")
    if api_base:
        arguments["api_base"] = api_base
    return LiteLLMModel(_required_model(), **arguments)


root_agent = Agent(
    name="hatchet_consumer",
    description="Submit and await externally executed report jobs.",
    model=_model(),
    instruction=(
        "Call create_report_job exactly once with topic 'quarterly'. "
        "Do not answer before the tool completes. After completion, copy the "
        "report field verbatim into a concise final answer."
    ),
)
