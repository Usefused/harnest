"""Provider-independent ticket routing questions and illustrative thresholds."""

from harnest.decisions import (
    Choice, ChoicePolicy, DecisionAction, DecisionDefinition, DecisionOutcome,
)


TRIAGE = DecisionDefinition(
    name="support_route",
    version="1",
    questions=(Choice(
        name="department",
        instructions="Which team should handle the customer message in `ticket`?",
        options={
            "billing": "Charges, invoices, payments, and refund requests",
            "support": "Product help, bugs, and technical problems",
            "other": "Anything else, mixed requests, or insufficient information",
        },
    ),),
)

ROUTING = ChoicePolicy(
    question="department",
    routes={
        "billing": DecisionOutcome(DecisionAction.ROUTE, "billing"),
        "support": DecisionOutcome(DecisionAction.ROUTE, "support"),
        "other": DecisionOutcome(DecisionAction.REVIEW),
    },
    # This is a demonstration threshold, not a calibration claim for real tickets.
    minimum_confidence=0.8,
    uncertain=DecisionOutcome(DecisionAction.REVIEW),
)
