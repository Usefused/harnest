import os

from harnest.agent import Agent
from harnest.model import LiteLLMModel


technical_specialist = Agent(
    name="technical_specialist",
    model=LiteLLMModel(
        os.getenv("LITELLM_MODEL", "ollama_chat/qwen3.5:cloud"),
        api_base=os.getenv("LITELLM_API_BASE", "http://127.0.0.1:11434"),
    ),
    description="Diagnoses technical API and integration problems.",
    instruction=(
        "Diagnose technical support issues. Ask for the smallest useful set of "
        "reproduction details, separate evidence from hypotheses, and return next steps."
    ),
)
