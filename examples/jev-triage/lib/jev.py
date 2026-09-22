"""An application-local Jev Choice adapter using the public decision contract."""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from harnest.decisions import (
    Choice, ChoiceResult, DecisionCapabilities, DecisionRequest,
    DecisionResponse, QuestionKind,
)

if TYPE_CHECKING:
    from typesafe_sdk import AsyncTypeSafeClient


def json_value(value: Any) -> Any:
    """Convert immutable Harnest state into the SDK's JSON container types."""
    if isinstance(value, Mapping):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [json_value(item) for item in value]
    return value


class JevProvider:
    """Evaluate Choice questions with a pinned model and lifecycle-owned client."""

    version = "jev-1.13.0"
    capabilities = DecisionCapabilities(
        frozenset({QuestionKind.CHOICE}),
        batching=True, probabilities=True, confidence=True,
    )

    def __init__(self, client: "AsyncTypeSafeClient") -> None:
        """Retain the caller's client without opening or owning another connection."""
        self.client = client

    async def evaluate(self, request: DecisionRequest) -> DecisionResponse:
        """Translate typed questions and preserve the complete native answer metadata."""
        from typesafe_sdk import Choice as JevChoice

        if any(not isinstance(q, Choice) for q in request.definition.questions):
            raise TypeError("This Jev adapter supports Choice questions only")
        result = await self.client.system_one(
            model=self.version,
            state=json_value(request.state),
            questions={
                q.name: JevChoice(instructions=q.instructions, criteria=dict(q.options))
                for q in request.definition.questions
            },
        )
        if result.model != self.version:
            raise ValueError("Jev returned a different model revision")
        return DecisionResponse({name: choice_result(answer) for name, answer in result.answers.items()})


def choice_result(answer: Any) -> ChoiceResult:
    """Reject non-choice answers before Harnest validates their requested domains."""
    from typesafe_sdk import ChoiceAnswer

    if not isinstance(answer, ChoiceAnswer):
        raise TypeError("Expected a Jev Choice answer")
    return ChoiceResult(answer.choice, answer.probabilities, answer.confidence)
