"""Refund agent whose payment tool participates in a shared Threadify contract."""

from harnest.agent import Agent
from harnest.model import LiteLLMModel


root_agent = Agent(
    name="contract_refund",
    model=LiteLLMModel.from_openai_environment(),
    description="Checks an order and requests a governed refund.",
    history="session",
)
