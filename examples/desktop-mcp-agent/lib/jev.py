"""Application-local adapter from Jev Choice answers to Harnest decisions."""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from harnest.decisions import (
    Choice, ChoiceResult, DecisionCapabilities, DecisionRequest,
    DecisionResponse, QuestionKind,
)

if TYPE_CHECKING:
    from typesafe_sdk import AsyncTypeSafeClient


def _json_value(value: Any) -> Any:
    """Convert immutable decision state into Jev's JSON container types."""
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


class JevProvider:
    """Evaluate one browser-use Choice with the runtime-owned Jev client."""

    version = "jev-1.13.0"
    capabilities = DecisionCapabilities(
        frozenset({QuestionKind.CHOICE}),
        batching=True, probabilities=True, confidence=True,
    )

    def __init__(self, client: "AsyncTypeSafeClient") -> None:
        """Borrow a client whose lifetime belongs to the application resource."""
        self.client = client

    async def evaluate(self, request: DecisionRequest) -> DecisionResponse:
        """Validate Jev's model revision and answer type before policy routing."""
        from typesafe_sdk import Choice as JevChoice, ChoiceAnswer

        if any(not isinstance(question, Choice) for question in request.definition.questions):
            raise TypeError("Jev browser adapter supports Choice questions only")
        result = await self.client.system_one(
            model=self.version,
            state=_json_value(request.state),
            questions={
                question.name: JevChoice(
                    instructions=question.instructions, criteria=dict(question.options)
                )
                for question in request.definition.questions
            },
        )
        if result.model != self.version:
            raise ValueError("Jev returned a different model revision")
        answers = {}
        for name, answer in result.answers.items():
            if not isinstance(answer, ChoiceAnswer):
                raise TypeError("Jev browser decision must be a Choice answer")
            answers[name] = ChoiceResult(answer.choice, answer.probabilities, answer.confidence)
        return DecisionResponse(answers)
