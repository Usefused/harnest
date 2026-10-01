from harnest.agent import Agent
from harnest.model import LiteLLMModel


root_agent = Agent(
    name="mcp_agent",
    history="session",
    model=LiteLLMModel.from_openai_environment(),
)
