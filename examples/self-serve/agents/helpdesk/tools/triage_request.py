from harnest.models.triage import TriageResult
from harnest.tool import tool


@tool
def triage_request(
    summary: str, production_blocked: bool = False
) -> TriageResult:
    """Recommend the queue and priority for a customer support request."""

    lowered = summary.lower()
    technical = any(
        word in lowered
        for word in ("api", "error", "integration", "authentication")
    )
    return TriageResult(
        queue="technical-support" if technical else "customer-success",
        priority="urgent" if production_blocked else "normal",
        reason=(
            "Production is blocked."
            if production_blocked
            else "Classified from the request summary."
        ),
    )
