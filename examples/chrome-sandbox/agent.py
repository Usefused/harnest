"""Managed agent with access to one named Chrome sandbox."""

from harnest.agent import Agent
from harnest.model import LiteLLMModel


root_agent = Agent(
    name="chrome_researcher",
    model=LiteLLMModel.from_openai_environment(),
    description="Reads approved public pages in an isolated Chromium browser.",
    history="session",
    sandboxes=["chrome"],
)
