"""Draft customer-facing text without giving the language model ownership of routing."""

import os

from harnest.agent import Agent, instruction_file
from harnest.graph import Event, GraphContext
from harnest.model import LiteLLMModel
from harnest.lib.decision_resources import offline_enabled
from harnest.models.result import SupportResponse, TriageResult


def reply_node():
    """Use a real managed LLM node live and an explicitly labeled callable offline."""
    if offline_enabled():
        return offline_reply
    options = {
        key: os.environ[variable] for key, variable in (
            ("api_base", "JEV_LLM_API_BASE"), ("api_key", "JEV_LLM_API_KEY"),
            ("reasoning_effort", "JEV_LLM_REASONING_EFFORT"),
        ) if os.getenv(variable)
    }
    # A placeholder keeps compilation credential-free; live runs select their provider explicitly.
    model = os.getenv("JEV_LLM_MODEL", "openai/your-model")
    return Agent(
        name="support_reply",
        history="turn",
        instruction=instruction_file(__file__, "../instructions.md"),
        model=LiteLLMModel(
            model, timeout=60, max_tokens=800, num_retries=0, **options,
        ),
    )


def offline_reply(payload: dict) -> str:
    """Make fixture runs fully offline, including the response-writing step."""
    decision = TriageResult.model_validate(payload["decision"])
    destination = f"the {decision.queue} queue" if decision.queue else "manual review"
    return f"Offline fixture — Your ticket is recommended for {destination}. No ticket has been sent."


def finish_reply(reply: str, context: GraphContext) -> Event:
    """Expose the LLM reply while keeping the routing decision in internal state."""
    if not isinstance(reply, str) or not reply.strip():
        raise ValueError("The reply model must return non-empty text")
    result = SupportResponse(reply=reply.strip())
    return Event(output=result.model_dump(), message=result.reply)
