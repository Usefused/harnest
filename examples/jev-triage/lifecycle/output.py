"""Keep Jev judgments internal while exposing the customer-facing LLM reply."""

from harnest import lifecycle
from harnest.output import OutputPolicy


@lifecycle.output_policy
def output_policy() -> OutputPolicy:
    """Set decision_results=True to include separate decision events in responses."""
    return OutputPolicy(decision_results=False)
