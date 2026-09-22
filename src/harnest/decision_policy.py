"""Pure decision policies produce outcomes without performing agent actions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from .decision_types import (
    Choice, ChoiceResult, DecisionCapabilities, DecisionDefinition, DecisionResponse,
    Predicate, PredicateResult, Score, ScoreResult, finite, identifier, probability,
)


class DecisionAction(str, Enum):
    """Authored control-flow signals; none grants permission to execute a tool."""

    ROUTE = "route"
    PROCEED = "proceed"
    BLOCK = "block"
    REVIEW = "review"
    REASON = "reason"
    ABSTAIN = "abstain"


@dataclass(frozen=True, slots=True)
class DecisionOutcome:
    """A policy action and, for routing, an authored destination identity."""

    action: DecisionAction
    route: str | None = None

    def __post_init__(self) -> None:
        """Keep route destinations explicit and reject contradictory outcomes."""
        if not isinstance(self.action, DecisionAction):
            raise TypeError("decision action must be DecisionAction")
        if self.action is DecisionAction.ROUTE:
            identifier(self.route)
        elif self.route is not None:
            raise ValueError("only route outcomes may have a destination")


_ABSTAIN = DecisionOutcome(DecisionAction.ABSTAIN)


def _outcome(value: DecisionOutcome) -> None:
    """Reject untyped policy branches before evaluation starts."""
    if not isinstance(value, DecisionOutcome):
        raise TypeError("decision policy branches must be DecisionOutcome")


@dataclass(frozen=True, slots=True)
class ChoicePolicy:
    """Map every allowed choice to an outcome, optionally requiring confidence."""

    question: str
    routes: Mapping[str, DecisionOutcome]
    minimum_confidence: float | None = None
    uncertain: DecisionOutcome = _ABSTAIN

    def __post_init__(self) -> None:
        """Freeze authored routing rules and retain explicit uncertainty behavior."""
        identifier(self.question)
        if not isinstance(self.routes, Mapping) or not self.routes:
            raise ValueError("choice policy requires routes")
        for key, value in self.routes.items():
            identifier(key)
            _outcome(value)
        _outcome(self.uncertain)
        if self.minimum_confidence is not None:
            probability(self.minimum_confidence)
        object.__setattr__(self, "routes", MappingProxyType(dict(self.routes)))

    def validate(self, definition: DecisionDefinition, capabilities: DecisionCapabilities) -> None:
        """Reject incomplete routes and unsupported confidence requirements at binding."""
        question = _find_question(definition, self.question)
        if not isinstance(question, Choice) or set(self.routes) != set(question.options):
            raise ValueError("choice policy routes must cover exactly the question options")
        if self.minimum_confidence is not None and not capabilities.confidence:
            raise ValueError("choice policy requires a provider with native confidence")

    def apply(self, response: DecisionResponse) -> DecisionOutcome:
        """Apply authored branches without interpreting confidence as accuracy."""
        answer = response.answers[self.question]
        if not isinstance(answer, ChoiceResult):
            raise TypeError("choice policy requires ChoiceResult")
        if self.minimum_confidence is not None:
            if answer.confidence is None or answer.confidence < self.minimum_confidence:
                return self.uncertain
        return self.routes[answer.value]


@dataclass(frozen=True, slots=True)
class ThresholdPolicy:
    """Use inclusive lower/upper boundaries and an explicit middle uncertainty band."""

    question: str
    lower: float
    upper: float
    below: DecisionOutcome = DecisionOutcome(DecisionAction.BLOCK)
    above: DecisionOutcome = DecisionOutcome(DecisionAction.PROCEED)
    uncertain: DecisionOutcome = _ABSTAIN

    def __post_init__(self) -> None:
        """Require separated thresholds so equality never selects two branches."""
        identifier(self.question)
        finite(self.lower, minimum=0)
        finite(self.upper, minimum=0)
        if self.lower >= self.upper:
            raise ValueError("decision lower threshold must be less than upper")
        for value in (self.below, self.above, self.uncertain):
            _outcome(value)

    def validate(self, definition: DecisionDefinition, capabilities: DecisionCapabilities) -> None:
        """Check thresholds against the actual probability or rubric scale."""
        question = _find_question(definition, self.question)
        if not isinstance(question, (Score, Predicate)):
            raise ValueError("threshold policies require Score or Predicate questions")
        maximum = len(question.levels) - 1 if isinstance(question, Score) else 1
        finite(self.upper, maximum=maximum)

    def apply(self, response: DecisionResponse) -> DecisionOutcome:
        """Return a signal on the question's native scale without running effects."""
        answer = response.answers[self.question]
        if not isinstance(answer, (ScoreResult, PredicateResult)):
            raise TypeError("threshold policy requires ScoreResult or PredicateResult")
        value = answer.probability if isinstance(answer, PredicateResult) else answer.value
        if value <= self.lower:
            return self.below
        if value >= self.upper:
            return self.above
        return self.uncertain


def _find_question(definition: DecisionDefinition, name: str) -> Choice | Score | Predicate:
    """Resolve policy references from the immutable definition only."""
    for question in definition.questions:
        if question.name == name:
            return question
    raise ValueError("decision policy references an unknown question")


@dataclass(frozen=True, slots=True)
class DecisionBinding:
    """Bind a reusable definition to a provider name and optional pure policy."""

    definition: DecisionDefinition
    provider: str
    policy: ChoicePolicy | ThresholdPolicy | None = None
    on_error: DecisionOutcome | None = field(default=None)

    def __post_init__(self) -> None:
        """Allow failure escalation while preventing automatic authorization on failure."""
        if not isinstance(self.definition, DecisionDefinition):
            raise TypeError("binding definition must be DecisionDefinition")
        identifier(self.provider)
        if self.policy is not None and not isinstance(self.policy, (ChoicePolicy, ThresholdPolicy)):
            raise TypeError("decision policy must be ChoicePolicy or ThresholdPolicy")
        if self.on_error is not None:
            _outcome(self.on_error)
            if self.on_error.action not in {DecisionAction.ABSTAIN, DecisionAction.BLOCK,
                                           DecisionAction.REVIEW, DecisionAction.REASON}:
                raise ValueError("decision failures may only abstain, block, review or request reasoning")
