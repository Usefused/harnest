"""Immutable provider-neutral questions and answers for bounded judgments."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
import re
from types import MappingProxyType
from typing import Any, Mapping, Protocol, runtime_checkable


class DecisionError(RuntimeError):
    """A decision could not be evaluated without violating its contract."""


class DecisionProviderError(DecisionError):
    """A provider failed; its private exception is intentionally not exposed."""


class DecisionTimeoutError(DecisionError):
    """A decision exceeded its configured evaluation deadline."""


class DecisionValidationError(DecisionError):
    """A provider response did not match the declared decision contract."""


class QuestionKind(str, Enum):
    """Portable question shapes a decision provider can explicitly support."""

    CHOICE = "choice"
    SCORE = "score"
    PREDICATE = "predicate"


def identifier(value: str) -> None:
    """Keep authored identities bounded and suitable for telemetry labels."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        raise ValueError("decision identities require 1–64 letters, digits, dots, underscores or hyphens")


def finite(value: float, *, minimum: float = -math.inf, maximum: float = math.inf) -> None:
    """Reject booleans, non-finite numbers and values outside the declared scale."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("decision values must be real numbers")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError("decision value is outside its finite range")


def probability(value: float) -> None:
    """Validate probability or provider confidence without assuming calibration."""
    finite(value, minimum=0, maximum=1)


def _question(name: str, instructions: str) -> None:
    """Validate question identity separately from its private instructions."""
    identifier(name)
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("decision question instructions must be nonempty text")


@dataclass(frozen=True, slots=True)
class Choice:
    """Choose one key from an unordered map of labels and descriptions."""

    name: str
    instructions: str = field(repr=False)
    options: Mapping[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        """Snapshot the answer space so later edits cannot change a live decision."""
        _question(self.name, self.instructions)
        if not isinstance(self.options, Mapping) or len(self.options) < 2:
            raise ValueError("choice questions require at least two options")
        for key, description in self.options.items():
            identifier(key)
            if not isinstance(description, str):
                raise TypeError("choice descriptions must be text")
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))


@dataclass(frozen=True, slots=True)
class Score:
    """Judge a rubric on the numeric scale 0 through len(levels) minus one."""

    name: str
    instructions: str = field(repr=False)
    levels: tuple[str, ...] = field(repr=False)

    def __post_init__(self) -> None:
        """Require an ordered rubric whose fractional results have a defined scale."""
        _question(self.name, self.instructions)
        if not isinstance(self.levels, tuple) or len(self.levels) < 2:
            raise ValueError("score questions require a tuple of at least two levels")
        if any(not isinstance(level, str) or not level.strip() for level in self.levels):
            raise ValueError("score levels must be nonempty text")
        if len(set(self.levels)) != len(self.levels):
            raise ValueError("score levels must be distinct")


@dataclass(frozen=True, slots=True)
class Predicate:
    """Judge a proposition by returning the probability that it is true."""

    name: str
    instructions: str = field(repr=False)

    def __post_init__(self) -> None:
        """Validate a proposition without converting its probability to a boolean."""
        _question(self.name, self.instructions)


DecisionQuestion = Choice | Score | Predicate


def question_kind(question: DecisionQuestion) -> QuestionKind:
    """Resolve only supported question contracts, never arbitrary provider types."""
    kinds = {Choice: QuestionKind.CHOICE, Score: QuestionKind.SCORE, Predicate: QuestionKind.PREDICATE}
    try:
        return kinds[type(question)]
    except KeyError:
        raise TypeError("decision questions must be Choice, Score or Predicate") from None


@dataclass(frozen=True, slots=True)
class DecisionDefinition:
    """A versioned set of independent questions, separate from provider selection."""

    name: str
    version: str
    questions: tuple[DecisionQuestion, ...]

    def __post_init__(self) -> None:
        """Reject ambiguous answer identities before any provider is called."""
        identifier(self.name)
        identifier(self.version)
        if not isinstance(self.questions, tuple) or not self.questions:
            raise ValueError("decisions require a nonempty tuple of questions")
        for question in self.questions:
            question_kind(question)
        names = [question.name for question in self.questions]
        if len(set(names)) != len(names):
            raise ValueError("decision question names must be unique")


def _freeze_state(value: Any, parents: frozenset[int] = frozenset()) -> Any:
    """Snapshot JSON state recursively, rejecting cycles and non-JSON objects."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        finite(value)
        return value
    if id(value) in parents:
        raise ValueError("decision state cannot contain cycles")
    ancestors = parents | {id(value)}
    if isinstance(value, Mapping):
        return _freeze_mapping(value, ancestors)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_state(item, ancestors) for item in value)
    raise TypeError("decision state must contain only JSON-compatible values")


def _freeze_mapping(value: Mapping[str, Any], parents: frozenset[int]) -> Mapping[str, Any]:
    """Keep state keys textual without coercing or exposing private values."""
    if any(not isinstance(key, str) for key in value):
        raise TypeError("decision state keys must be strings")
    return MappingProxyType({key: _freeze_state(item, parents) for key, item in value.items()})


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    """A definition and isolated state snapshot sent to one provider evaluation."""

    definition: DecisionDefinition
    state: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        """Prevent a provider from mutating caller-owned state or question definitions."""
        if not isinstance(self.definition, DecisionDefinition):
            raise TypeError("request definition must be DecisionDefinition")
        if not isinstance(self.state, Mapping):
            raise TypeError("decision state must be a mapping")
        object.__setattr__(self, "state", _freeze_state(self.state))


@dataclass(frozen=True, slots=True)
class ChoiceResult:
    """A selected option with optional native distribution and confidence."""

    value: str = field(repr=False)
    probabilities: Mapping[str, float] | None = field(default=None, repr=False)
    confidence: float | None = None

    def __post_init__(self) -> None:
        """Snapshot optional distributions; request-dependent checks run at evaluation."""
        identifier(self.value)
        if self.probabilities is not None:
            if not isinstance(self.probabilities, Mapping):
                raise TypeError("choice probabilities must be a mapping")
            _distribution(tuple(self.probabilities.values()))
            object.__setattr__(self, "probabilities", MappingProxyType(dict(self.probabilities)))
        if self.confidence is not None:
            probability(self.confidence)


def _distribution(values: tuple[float, ...]) -> None:
    """Validate a complete probability distribution with a small rounding tolerance."""
    for value in values:
        probability(value)
    if not math.isclose(sum(values), 1, rel_tol=0, abs_tol=1e-6):
        raise ValueError("decision probabilities must sum to one")


@dataclass(frozen=True, slots=True)
class ScoreResult:
    """A rubric position with optional probabilities in the declared level order."""

    value: float = field(repr=False)
    probabilities: tuple[float, ...] | None = field(default=None, repr=False)
    confidence: float | None = None

    def __post_init__(self) -> None:
        """Validate finite scores without assuming a provider's scoring formula."""
        finite(self.value, minimum=0)
        if self.probabilities is not None:
            if not isinstance(self.probabilities, tuple):
                raise TypeError("score probabilities must be a tuple")
            _distribution(self.probabilities)
        if self.confidence is not None:
            probability(self.confidence)


@dataclass(frozen=True, slots=True)
class PredicateResult:
    """The probability of true; this is not a generic confidence score."""

    probability: float = field(repr=False)

    def __post_init__(self) -> None:
        """Preserve the meaning of uncertain, true and false probabilities."""
        probability(self.probability)


DecisionAnswer = ChoiceResult | ScoreResult | PredicateResult


@dataclass(frozen=True, slots=True)
class DecisionResponse:
    """Typed answers keyed by the exact question identities in the request."""

    answers: Mapping[str, DecisionAnswer] = field(repr=False)

    def __post_init__(self) -> None:
        """Detach response mappings from provider-owned mutable containers."""
        if not isinstance(self.answers, Mapping):
            raise TypeError("decision answers must be a mapping")
        for key, value in self.answers.items():
            identifier(key)
            if type(value) not in {ChoiceResult, ScoreResult, PredicateResult}:
                raise TypeError("decision answers require typed result values")
        object.__setattr__(self, "answers", MappingProxyType(dict(self.answers)))


@dataclass(frozen=True, slots=True)
class DecisionCapabilities:
    """Provider guarantees checked before evaluation, without guessing calibration."""

    kinds: frozenset[QuestionKind]
    batching: bool = False
    probabilities: bool = False
    confidence: bool = False

    def __post_init__(self) -> None:
        """Require explicit typed guarantees; strings and truthy objects are invalid."""
        if not isinstance(self.kinds, frozenset) or not self.kinds:
            raise TypeError("provider kinds must be a nonempty frozenset")
        if any(not isinstance(kind, QuestionKind) for kind in self.kinds):
            raise TypeError("provider kinds must contain QuestionKind values")
        if any(type(flag) is not bool for flag in (self.batching, self.probabilities, self.confidence)):
            raise TypeError("provider capability flags must be booleans")


@runtime_checkable
class DecisionProvider(Protocol):
    """Implement in application code or an extension; lifecycle owns the client."""

    capabilities: DecisionCapabilities
    version: str

    async def evaluate(self, request: DecisionRequest) -> DecisionResponse:
        """Evaluate independent questions without executing the selected action."""
        ...
