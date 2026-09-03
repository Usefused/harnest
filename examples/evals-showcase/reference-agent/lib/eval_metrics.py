from __future__ import annotations

from statistics import mean

from google.adk.evaluation.eval_case import Invocation
from google.adk.evaluation.eval_metrics import EvalMetric, EvalStatus
from google.adk.evaluation.evaluator import EvaluationResult, PerInvocationResult


def _final_text(invocation: Invocation) -> str:
    """Collect visible text from one invocation's final response."""

    content = invocation.final_response
    if content is None or not content.parts:
        return ""
    return " ".join(part.text for part in content.parts if part.text).casefold()


def verified_capital_present(
    metric: EvalMetric,
    actual: list[Invocation],
    expected: list[Invocation] | None,
    scenario: object | None,
) -> EvaluationResult:
    """Score whether every final answer contains the verified city and country."""

    del scenario
    threshold = metric.criterion.threshold if metric.criterion is not None else 1.0
    scores = []
    results = []
    for index, invocation in enumerate(actual):
        text = _final_text(invocation)
        score = float("paris" in text and "france" in text)
        scores.append(score)
        results.append(
            PerInvocationResult(
                actual_invocation=invocation,
                expected_invocation=(
                    expected[index] if expected is not None else None
                ),
                score=score,
                eval_status=(
                    EvalStatus.PASSED if score >= threshold else EvalStatus.FAILED
                ),
            )
        )
    overall = mean(scores) if scores else 0.0
    return EvaluationResult(
        overall_score=overall,
        overall_eval_status=(
            EvalStatus.PASSED if overall >= threshold else EvalStatus.FAILED
        ),
        per_invocation_results=results,
    )
