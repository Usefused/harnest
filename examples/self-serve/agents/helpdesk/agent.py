from harnest.agent import Agent
from harnest.model import OllamaModel


root_agent = Agent(
    name="helpdesk",
    history="session",
    model=OllamaModel.from_environment(),
    description="Answers product questions and triages support requests.",
)
