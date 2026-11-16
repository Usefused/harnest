"""Copy to helper.py and reference helper explicitly in the root Graph.

Import with from subagents.helper import helper, then add it to Graph.nodes
and connect it with an Edge. A flat subagent has no private resource folders.
"""

from harnest.agent import Agent
from harnest.model import LiteLLMModel


helper = Agent(
    name="helper",
    model=LiteLLMModel.from_openai_environment(),
    instruction="Summarize the request clearly; acknowledge missing information.",
)
