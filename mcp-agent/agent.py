from harnest.agent import Agent
from harnest.model import OllamaModel


root_agent = Agent(
    name="mcp_agent",
    history="session",
    model=OllamaModel.from_environment(),
)
