"""Turn a validated decision into a typed, internal queue recommendation."""

from harnest import context
from harnest.decisions import ChoiceResult, DecisionAction, DecisionEvaluation
from harnest.graph import Event
from harnest.models.result import TriageResult


def recommendation(evaluation: DecisionEvaluation) -> TriageResult:
    """Keep failure, uncertainty and offline provenance explicit in the internal recommendation."""
    answer = None if evaluation.response is None else evaluation.response.answers["department"]
    if answer is not None and not isinstance(answer, ChoiceResult):
        raise TypeError("Ticket triage requires a ChoiceResult")
    routed = evaluation.outcome.action is DecisionAction.ROUTE
    reason = _reason(evaluation, answer, routed)
    return TriageResult(
        action="route" if routed else "review",
        queue=evaluation.outcome.route if routed else None,
        department=None if answer is None else answer.value,
        confidence=None if answer is None else answer.confidence,
        reason=reason,
        mode="offline" if evaluation.provider == "fixture" else "live",
        provider=evaluation.provider,
        provider_version=evaluation.provider_version,
        error=evaluation.error,
    )


def _reason(evaluation: DecisionEvaluation, answer: ChoiceResult | None, routed: bool) -> str:
    """Explain authored policy without inventing a model-generated rationale."""
    if evaluation.error is not None:
        return "evaluation_failed"
    if routed:
        return "classified"
    if answer is not None and answer.value == "other":
        return "other"
    return "low_confidence"


async def triage_ticket(ticket: str) -> Event:
    """Classify once and keep authoritative routing separate from the LLM's draft."""
    evaluation = await context.decisions.evaluate("support_route", {"ticket": ticket})
    result = recommendation(evaluation)
    decision = result.model_dump()
    return Event(
        output={"ticket": ticket, "decision": decision},
        state_delta={"jev_triage_decision": decision},
    )
