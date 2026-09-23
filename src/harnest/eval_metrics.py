"""Small authored metric contracts adapted to the existing ADK evaluator."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import wraps
import inspect
import math
from numbers import Real
from statistics import mean
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from google.adk.evaluation.conversation_scenarios import ConversationScenario
    from google.adk.evaluation.eval_case import Invocation
    from google.adk.evaluation.eval_metrics import EvalMetric


@dataclass(frozen=True)
class MetricScore:
    """One finite score and optional human-readable evidence; None means unscored."""

    score: float | None
    evidence: str | None = None

    def __post_init__(self) -> None:
        """Reject ambiguous or non-finite service results before they reach CI gates."""
        if self.score is not None:
            if isinstance(self.score, bool) or not isinstance(self.score, Real):
                raise TypeError("metric score must be a finite number or None")
            if not math.isfinite(self.score):
                raise ValueError("metric score must be finite")
        if self.evidence is not None and not isinstance(self.evidence, str):
            raise TypeError("metric evidence must be a string or None")


@dataclass(frozen=True)
class MetricContext:
    """A case's native ADK evidence, available to sync and async Harnest scorers."""

    metric: EvalMetric
    actual: tuple[Invocation, ...]
    expected: tuple[Invocation, ...] | None = None
    scenario: ConversationScenario | None = None

    @property
    def threshold(self) -> float:
        """Read the authored criterion instead of ADK's deprecated threshold field."""
        return self.metric.criterion.threshold


def metric(function: Callable[[MetricContext], Any]) -> Callable[..., Any]:
    """Adapt a context-based scorer to ADK's four-argument custom metric contract.

    Return a MetricScore for the whole conversation, a sequence with one score
    per actual invocation, or a native ADK EvaluationResult. Native ADK scorers
    can also remain undecorated and register through the same customMetrics map.
    """
    if not callable(function):
        raise TypeError("metric requires a callable scorer")

    @wraps(function)
    async def evaluate(eval_metric, actual, expected=None, scenario=None):
        """Await authored service calls without owning their clients or credentials."""
        context = MetricContext(
            eval_metric,
            tuple(actual),
            None if expected is None else tuple(expected),
            scenario,
        )
        result = function(context)
        if inspect.isawaitable(result):
            result = await result
        return _evaluation_result(context, result)

    return evaluate


def _turn_scores(result: Any, count: int) -> tuple[MetricScore, ...]:
    """Represent conversation scores once, on the final turn, without inflating means."""
    if isinstance(result, MetricScore):
        if not count:
            raise ValueError(
                "a conversation metric requires at least one actual invocation"
            )
        return (MetricScore(None),) * (count - 1) + (result,)
    if isinstance(result, (str, bytes)) or not isinstance(result, Sequence):
        raise TypeError(
            "metric must return MetricScore, a score sequence, or ADK EvaluationResult"
        )
    scores = tuple(result)
    if len(scores) != count:
        raise ValueError("metric must return one score per actual invocation")
    if not all(isinstance(score, MetricScore) for score in scores):
        raise TypeError("per-invocation metric results must be MetricScore values")
    return scores


def _status(score: float | None, threshold: float) -> Any:
    """Apply the same inclusive threshold to aggregate and per-turn scores."""
    from google.adk.evaluation.eval_metrics import EvalStatus

    if score is None:
        return EvalStatus.NOT_EVALUATED
    return EvalStatus.PASSED if score >= threshold else EvalStatus.FAILED


def _evidence(metric_name: str, score: MetricScore) -> list[Any] | None:
    """Use ADK's retained rubric evidence so normal CLI reports preserve explanations."""
    from google.adk.evaluation.eval_rubrics import RubricScore

    if score.evidence is None:
        return None
    return [
        RubricScore(rubric_id=metric_name, score=score.score, rationale=score.evidence)
    ]


def _invocation_result(context: MetricContext, index: int, score: MetricScore) -> Any:
    """Pair actual evidence with an optional golden turn without truncating results."""
    from google.adk.evaluation.evaluator import PerInvocationResult

    expected = context.expected
    return PerInvocationResult(
        actual_invocation=context.actual[index],
        expected_invocation=expected[index]
        if expected and index < len(expected)
        else None,
        score=score.score,
        eval_status=_status(score.score, context.threshold),
        rubric_scores=_evidence(context.metric.metric_name, score),
    )


def _evaluation_result(context: MetricContext, result: Any) -> Any:
    """Convert convenience scores once, leaving native ADK results untouched."""
    from google.adk.evaluation.evaluator import EvaluationResult

    if isinstance(result, EvaluationResult):
        return result
    scores = _turn_scores(result, len(context.actual))
    values = [item.score for item in scores if item.score is not None]
    overall = mean(values) if values else None
    # ADK's gate uses the mean of scored turns. Use that exact policy in the
    # overall result too, including NOT_EVALUATED when nothing was scored.
    return EvaluationResult(
        overall_score=overall,
        overall_eval_status=_status(overall, context.threshold),
        per_invocation_results=[
            _invocation_result(context, index, score)
            for index, score in enumerate(scores)
        ],
        overall_rubric_scores=(
            _evidence(context.metric.metric_name, result)
            if isinstance(result, MetricScore)
            else None
        ),
    )
