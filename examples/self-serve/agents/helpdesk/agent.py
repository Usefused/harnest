import os

from harnest.agent import Agent
from harnest.model import LiteLLMModel


root_agent = Agent(
    name="helpdesk",
    history="session",
    model=LiteLLMModel(
        os.getenv("LITELLM_MODEL", "ollama_chat/qwen3.5:cloud"),
        api_base=os.getenv("LITELLM_API_BASE", "http://127.0.0.1:11434"),
    ),
    description="Answers product questions and triages support requests.",
)
