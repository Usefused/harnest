"""Studio's managed Harnest authoring agent, compiled with the shipped skills."""

import os

from harnest.agent import Agent
from harnest.model import LiteLLMModel
from harnest.lib.builder_settings import AssistantLimits, provider_options


root_agent = Agent(
    name="studio_builder",
    description="Proposes grounded Harnest source edits for explicit user review.",
    history="turn",
    model=LiteLLMModel(
        os.getenv("HARNEST_BUILDER_MODEL", "ollama_chat/qwen3.5:cloud"),
        **provider_options(),
        **AssistantLimits.from_environment().model_dump(),
    ),
)
