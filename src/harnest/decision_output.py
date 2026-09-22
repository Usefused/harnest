"""Invocation-owned projection of explicitly disclosed decision results."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, AsyncIterator

from .decision_types import ChoiceResult, DecisionAnswer, PredicateResult

if TYPE_CHECKING:
    from .decision_runtime import DecisionEvaluation


class DecisionOutput:
    """Buffer public judgments without retaining hidden state or provider payloads."""

    def __init__(self, *, enabled: bool = False) -> None:
        """Give each invocation a separate queue shared only with its child scopes."""
        self.enabled = enabled
        self._events: list[dict[str, Any]] = []

    def record(self, evaluation: DecisionEvaluation, *, agent: str) -> None:
        """Serialize only opted-in validated results, never the decision request."""
        if self.enabled:
            self._events.append({
                "type": "decision_result", "agent": agent,
                "value": _evaluation_value(evaluation),
            })

    def drain(self) -> list[dict[str, Any]]:
        """Transfer ownership so every result is emitted at most once."""
        events, self._events = self._events, []
        return events

    async def stream(self, iterator: AsyncIterator[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
        """Flush completed judgments before the next native event and at normal EOF."""
        # The lifecycle wrapper advances this iterator inside the invocation scope.
        # Closing it must also close the backend on disconnect or cancellation.
        try:
            async for event in iterator:
                for decision in self.drain():
                    yield decision
                yield event
            for decision in self.drain():
                yield decision
        finally:
            closer = getattr(iterator, "aclose", None)
            if callable(closer):
                await closer()


def _evaluation_value(evaluation: DecisionEvaluation) -> dict[str, Any]:
    """Keep the public envelope independent of provider-specific response objects."""
    outcome = evaluation.outcome
    response = evaluation.response
    return {
        "decision": evaluation.decision,
        "version": evaluation.version,
        "provider": evaluation.provider,
        "providerVersion": evaluation.provider_version,
        "answers": None if response is None else {
            name: _answer_value(answer) for name, answer in response.answers.items()
        },
        "outcome": None if outcome is None else {"action": outcome.action.value, "route": outcome.route},
        "durationSeconds": evaluation.duration_seconds,
        "error": evaluation.error,
    }


def _answer_value(answer: DecisionAnswer) -> dict[str, Any]:
    """Preserve the distinct meanings of choice, rubric score and probability."""
    if isinstance(answer, PredicateResult):
        return {"kind": "predicate", "probability": answer.probability}
    value: dict[str, Any] = {
        "kind": "choice" if isinstance(answer, ChoiceResult) else "score",
        "value": answer.value,
        "confidence": answer.confidence,
    }
    if answer.probabilities is not None:
        value["probabilities"] = (
            dict(answer.probabilities) if isinstance(answer, ChoiceResult)
            else list(answer.probabilities)
        )
    return value
