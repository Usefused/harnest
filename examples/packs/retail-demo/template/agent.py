from harnest.agent import Agent
from harnest.model import LiteLLMModel

root_agent = Agent(name="retail_lab", model=LiteLLMModel("openai/gpt-5.5"))
