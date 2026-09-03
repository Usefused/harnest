from harnest.agent import Agent
from harnest.model import LiteLLMModel


root_agent = Agent(
    name="city_facts_reference",
    model=LiteLLMModel.from_openai_environment(),
    description="Answers small factual questions using a trusted local lookup.",
    history="session",
)
