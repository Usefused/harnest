from harnest.agent import Agent
from harnest.model import LiteLLMModel


user_Agent = Agent(
    name="user_Agent",
    model=LiteLLMModel.from_openai_environment(),
    description="user_Agent capability",
    instruction="user_Agent capability",
)
