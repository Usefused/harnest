from harnest.agent import Agent
from harnest.model import LiteLLMModel

root_agent = Agent(name="support_assistant", model=LiteLLMModel.from_openai_environment(), instruction="Help employees use approved company services.")
