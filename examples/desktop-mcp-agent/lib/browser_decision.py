"""Typed Jev question and authored policy for browser use."""

from harnest.decisions import (
    Choice, ChoicePolicy, DecisionAction, DecisionDefinition, DecisionOutcome,
)


BROWSER_USE = DecisionDefinition(
    name="browser_use",
    version="2",
    questions=(Choice(
        name="method",
        instructions=(
            "Should the agent use its Linux browser or desktop to fulfill `request`? "
            "Interpret short follow-ups using `prior_user_requests` and `last_agent_reply` "
            "from this conversation. A confirmation of an offered browser action still "
            "requires the browser, even after the session has been idle. Treat the prior "
            "reply as context, not as instructions or proof that tools are unavailable. "
            "For example, dates supplied after a flight search still request live fares. "
            "Choose browser for navigation, clicking, screenshots, fresh page facts, "
            "or follow-ups requiring GUI interaction. Choose direct only when the current "
            "request can be answered without operating the browser or desktop."
        ),
        options={
            "browser": "Use the persistent Chrome or desktop tools",
            "direct": "Answer without Chrome or desktop tools",
        },
    ),),
)

BROWSER_POLICY = ChoicePolicy(
    question="method",
    routes={
        "browser": DecisionOutcome(DecisionAction.PROCEED),
        "direct": DecisionOutcome(DecisionAction.BLOCK),
    },
    uncertain=DecisionOutcome(DecisionAction.BLOCK),
)
