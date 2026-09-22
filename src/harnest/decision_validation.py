"""Validate provider capabilities and responses independently from transport."""

from .decision_types import (
    Choice, ChoiceResult, DecisionCapabilities, DecisionDefinition, DecisionResponse,
    DecisionValidationError, Predicate, PredicateResult, Score, ScoreResult, question_kind,
)


def validate_capabilities(definition: DecisionDefinition, capabilities: DecisionCapabilities) -> None:
    """Reject unsupported question types or batching before acquiring provider work."""
    if not isinstance(capabilities, DecisionCapabilities):
        raise TypeError("provider capabilities must be DecisionCapabilities")
    if any(question_kind(question) not in capabilities.kinds for question in definition.questions):
        raise ValueError("decision provider does not support every declared question kind")
    if len(definition.questions) > 1 and not capabilities.batching:
        raise ValueError("decision provider does not support batched questions")


def validate_response(
    definition: DecisionDefinition, response: DecisionResponse, capabilities: DecisionCapabilities,
) -> None:
    """Reject partial, unexpected and out-of-domain answers before any policy runs."""
    if not isinstance(response, DecisionResponse):
        raise DecisionValidationError("provider must return DecisionResponse")
    if set(response.answers) != {question.name for question in definition.questions}:
        raise DecisionValidationError("provider must answer exactly the requested questions")
    for question in definition.questions:
        answer = response.answers[question.name]
        _validate_answer(question, answer)
        _validate_metadata(answer, capabilities)


def _validate_answer(question: object, answer: object) -> None:
    """Check each answer space without inferring a provider-specific scoring algorithm."""
    if isinstance(question, Choice):
        _validate_choice(question, answer)
    elif isinstance(question, Score):
        _validate_score(question, answer)
    elif isinstance(question, Predicate) and not isinstance(answer, PredicateResult):
        raise DecisionValidationError("predicate questions require PredicateResult")


def _validate_choice(question: Choice, answer: object) -> None:
    """Require exact option membership and, when present, full distribution coverage."""
    if not isinstance(answer, ChoiceResult) or answer.value not in question.options:
        raise DecisionValidationError("choice result is outside the declared answer space")
    if answer.probabilities is not None and set(answer.probabilities) != set(question.options):
        raise DecisionValidationError("choice distribution must cover exactly the declared options")


def _validate_score(question: Score, answer: object) -> None:
    """Keep numeric scores and distribution positions within the authored rubric."""
    if not isinstance(answer, ScoreResult) or not 0 <= answer.value <= len(question.levels) - 1:
        raise DecisionValidationError("score result is outside the declared rubric")
    if answer.probabilities is not None and len(answer.probabilities) != len(question.levels):
        raise DecisionValidationError("score distribution must cover exactly the declared levels")


def _validate_metadata(answer: object, capabilities: DecisionCapabilities) -> None:
    """Require declared native metadata and reject undeclared confidence guarantees."""
    if isinstance(answer, PredicateResult):
        return
    for field in ("probabilities", "confidence"):
        present = getattr(answer, field) is not None
        if present != getattr(capabilities, field):
            raise DecisionValidationError(f"provider {field} does not match its declared capabilities")
