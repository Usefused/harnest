"""Harnest's unified evaluation API: trajectories, errors, metrics, and runner contracts.

An evaluation engine in ``harnest.evaluators`` runs it; each framework adapter
names its engine with ``FrameworkAdapter.evaluator()``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._eval_runner_api import EvalRunnerContext, EvalRunnerResult
from .eval_metrics import MetricContext, MetricScore, metric

if TYPE_CHECKING:
    from .bundle import EvalSuite


EVAL_TRAJECTORIES = frozenset({"business", "strict"})


class EvaluationError(RuntimeError):
    """An evaluation request cannot be executed with the installed runtime."""


def require_eval_trajectory(trajectory: str) -> None:
    """Reject unknown tool-trajectory policies at every evaluation entrypoint."""

    if trajectory not in EVAL_TRAJECTORIES:
        raise EvaluationError("eval trajectory must be business or strict")


# The helpers below belong to the ADK evaluation engine, which runs Harnest's
# eval files today; they stay here because this module has always exported them.


def eval_config(suite: EvalSuite, trajectory: str) -> Any:
    """Load criteria with shared model defaults and the selected trajectory."""

    from .evaluators.adk.config import eval_config as adk_eval_config

    return adk_eval_config(suite, trajectory)


def eval_dependencies() -> tuple[Any, Any]:
    """Return the ADK evaluation engine's evaluator and eval-set classes."""

    from .evaluators.adk.config import eval_dependencies as adk_eval_dependencies

    return adk_eval_dependencies()


def supported_metric_names() -> tuple[str, ...]:
    """Return the built-in metric names the ADK evaluation engine supports."""

    from .evaluators.adk.config import supported_metric_names as adk_metric_names

    return adk_metric_names()


__all__ = [
    "EVAL_TRAJECTORIES",
    "EvaluationError",
    "EvalRunnerContext",
    "EvalRunnerResult",
    "MetricContext",
    "MetricScore",
    "metric",
    "eval_config",
    "eval_dependencies",
    "require_eval_trajectory",
    "supported_metric_names",
]
