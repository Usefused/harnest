"""One managed agent attached to an application-owned Linux desktop."""

from harnest.agent import Agent
from harnest.model import LiteLLMModel


root_agent = Agent(
    name="desktop_operator",
    model=LiteLLMModel.from_openai_environment(),
    description="Operates a persistent Linux desktop and its Chrome browser.",
    history="session",
)
