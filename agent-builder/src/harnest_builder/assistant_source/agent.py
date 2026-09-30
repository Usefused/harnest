"""Studio's managed Harnest authoring agent, compiled with the shipped skills."""

import os

from harnest.agent import Agent
from harnest.model import LiteLLMModel
from harnest.lib.builder_settings import AssistantLimits


root_agent = Agent(
    name="studio_builder",
    description="Proposes grounded Harnest source edits for explicit user review.",
    history="turn",
    model=LiteLLMModel(
        os.getenv("HARNEST_BUILDER_MODEL", "ollama_chat/qwen3.5:cloud"),
        **{key: os.environ[value] for key, value in (
            ("api_base", "HARNEST_BUILDER_API_BASE"),
            ("api_key", "HARNEST_BUILDER_API_KEY"),
        ) if os.getenv(value)},
        **AssistantLimits.from_environment().model_dump(),
    ),
)
