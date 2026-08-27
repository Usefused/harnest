from typing import Literal

from harnest.tool import tool
from typing_extensions import TypedDict


class TriageResult(TypedDict):
    queue: str
    priority: Literal["low", "normal", "high", "urgent"]
    reason: str


@tool
def triage_request(summary: str, production_blocked: bool = False) -> TriageResult:
    """Recommend the queue and priority for a customer support request."""

    lowered = summary.lower()
    technical = any(
        word in lowered
        for word in ("api", "error", "integration", "authentication")
    )
    return {
        "queue": "technical-support" if technical else "customer-success",
        "priority": "urgent" if production_blocked else "normal",
        "reason": (
            "Production is blocked."
            if production_blocked
            else "Classified from the request summary."
        ),
    }
