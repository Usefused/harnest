"""Shared evaluation policy for CLI and playground execution."""

from __future__ import annotations

from contextlib import contextmanager
import sys
from types import ModuleType
from typing import Any, Iterator
import uuid

from .bundle import EvalSuite


EVAL_TRAJECTORIES = frozenset({"business", "strict"})


class EvaluationError(RuntimeError):
    """An evaluation request cannot be executed with the installed runtime."""


def require_eval_trajectory(trajectory: str) -> None:
    """Reject unknown tool-trajectory policies at every evaluation entrypoint."""

    if trajectory not in EVAL_TRAJECTORIES:
        raise EvaluationError("eval trajectory must be business or strict")


def eval_config(suite: EvalSuite, trajectory: str) -> Any:
    """Load authored criteria and change only the selected trajectory policy."""

    require_eval_trajectory(trajectory)
    from google.adk.evaluation.eval_config import (
        get_evaluation_criteria_or_default,
    )

    config = get_evaluation_criteria_or_default(
        str(suite.config) if suite.config is not None else None
    )
    criterion = config.criteria.get("tool_trajectory_avg_score")
    if criterion is None:
        return config
    from google.adk.evaluation.eval_metrics import ToolTrajectoryCriterion

    threshold = criterion if isinstance(criterion, float) else criterion.threshold
    match_type = "IN_ORDER" if trajectory == "business" else "EXACT"
    config.criteria["tool_trajectory_avg_score"] = ToolTrajectoryCriterion(
        threshold=threshold,
        match_type=match_type,
    )
    return config


def eval_dependencies() -> tuple[Any, Any]:
    """Load ADK's evaluator and EvalSet model for either framework adapter."""

    try:
        from google.adk.evaluation.agent_evaluator import AgentEvaluator
        from google.adk.evaluation.eval_set import EvalSet
    except ImportError as exc:  # pragma: no cover - declared framework extras
        raise EvaluationError(
            "Google ADK evaluation dependencies are required for evals"
        ) from exc
    return AgentEvaluator, EvalSet


def customer_facing_eval_output_plugin() -> Any:
    """Remove private response parts before ADK metrics inspect model output."""

    from google.adk.plugins import BasePlugin
    from .runtime_adk import _customer_facing_parts

    class CustomerFacingEvalOutputPlugin(BasePlugin):
        def __init__(self) -> None:
            super().__init__(name="_harnest_customer_facing_eval_output")

        async def on_event_callback(
            self, *, invocation_context: Any, event: Any
        ) -> None:
            """Mutate evaluator-owned events without exposing hidden parts."""

            del invocation_context
            content = getattr(event, "content", None)
            parts = getattr(content, "parts", None)
            if parts is None:
                return
            visible_parts = list(_customer_facing_parts(parts))
            if len(visible_parts) != len(parts):
                content.parts = visible_parts

    return CustomerFacingEvalOutputPlugin()


@contextmanager
def adk_eval_agent_module(application: Any) -> Iterator[str]:
    """Expose an isolated ADK application copy through its evaluator protocol."""

    package_name = f"_harnest_adk_eval_{uuid.uuid4().hex}"
    module_name = f"{package_name}.agent"
    package = ModuleType(package_name)
    package.__path__ = []
    module = ModuleType(module_name)
    module.root_agent = application.target
    module.app = _evaluation_app(application)
    sys.modules[package_name] = package
    sys.modules[module_name] = module
    try:
        yield module_name
    finally:
        sys.modules.pop(module_name, None)
        sys.modules.pop(package_name, None)


def _evaluation_app(application: Any) -> Any:
    """Copy the native App so filtering never mutates live request plugins."""

    app = application.native_app
    if app is None:
        return None
    plugins = [customer_facing_eval_output_plugin(), *list(app.plugins)]
    return app.model_copy(update={"plugins": plugins})


def supported_metric_names() -> tuple[str, ...]:
    """Reflect the installed ADK metric registry instead of freezing an allowlist."""

    from google.adk.evaluation.eval_metrics import PrebuiltMetrics

    return tuple(sorted(metric.value for metric in PrebuiltMetrics))


__all__ = [
    "EVAL_TRAJECTORIES",
    "EvaluationError",
    "adk_eval_agent_module",
    "eval_config",
    "eval_dependencies",
    "require_eval_trajectory",
    "supported_metric_names",
]
