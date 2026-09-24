"""Typed Jev question and authored policy for browser use."""

from harnest.decisions import (
    Choice, ChoicePolicy, DecisionAction, DecisionDefinition, DecisionOutcome,
)


BROWSER_USE = DecisionDefinition(
    name="browser_use",
    version="1",
    questions=(Choice(
        name="method",
        instructions=(
            "Should the agent use its Linux browser or desktop to fulfill `request`? "
            "Interpret short follow-ups using `prior_user_requests` from this conversation. "
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
