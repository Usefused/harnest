from harnest.agent import Agent
from harnest.model import LiteLLMModel


root_agent = Agent(
    name="helpdesk",
    history="session",
    model=LiteLLMModel.from_openai_environment(),
    description="Answers product questions and triages support requests.",
)
