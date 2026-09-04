"""Shared evaluation policy for CLI and playground execution."""

from __future__ import annotations

from contextlib import contextmanager
import json
import sys
from types import ModuleType
from typing import Any, Iterator
import uuid

from .bundle import EvalSuite
from .model import _openai_model_name_from_environment


EVAL_TRAJECTORIES = frozenset({"business", "strict"})
_LLM_JUDGE_METRICS = frozenset(
    {
        "final_response_match_v2",
        "rubric_based_final_response_quality_v1",
        "hallucinations_v1",
        "rubric_based_tool_use_quality_v1",
        "per_turn_user_simulator_quality_v1",
        "rubric_based_multi_turn_trajectory_quality_v1",
    }
)


class EvaluationError(RuntimeError):
    """An evaluation request cannot be executed with the installed runtime."""


def require_eval_trajectory(trajectory: str) -> None:
    """Reject unknown tool-trajectory policies at every evaluation entrypoint."""

    if trajectory not in EVAL_TRAJECTORIES:
        raise EvaluationError("eval trajectory must be business or strict")


def _eval_config_payload(suite: EvalSuite) -> dict[str, Any] | None:
    """Read the authored eval configuration before ADK applies model defaults."""

    if suite.config is None:
        return None
    return json.loads(suite.config.read_text(encoding="utf-8"))


def _set_default_judge_model(criterion: Any) -> Any:
    """Add the canonical judge model while preserving an authored override."""

    if isinstance(criterion, (int, float)):
        return {
            "threshold": float(criterion),
            "judgeModelOptions": {
                "judgeModel": _openai_model_name_from_environment()
            },
        }
    if not isinstance(criterion, dict):
        return criterion
    options_key = (
        "judgeModelOptions"
        if "judge_model_options" not in criterion
        else "judge_model_options"
    )
    options = criterion.setdefault(options_key, {})
    if isinstance(options, dict):
        model_key = "judgeModel" if "judge_model" not in options else "judge_model"
        # Resolve lazily so explicit custom models do not depend on an unused
        # OPENAI_MODEL value in the process environment.
        if model_key not in options:
            options[model_key] = _openai_model_name_from_environment()
    return criterion


def _apply_openai_eval_model_defaults(
    payload: dict[str, Any], suite: EvalSuite
) -> dict[str, Any]:
    """Route implicit judge and simulator models through the agent model contract."""

    criteria = payload.get("criteria")
    if isinstance(criteria, dict):
        for name, criterion in criteria.items():
            # This is a defaults policy, not a supported-metric allowlist.
            # Future/custom criteria with judge options receive the same
            # default without changing unrelated static or service metrics.
            if _uses_judge_model(name, criterion):
                criteria[name] = _set_default_judge_model(criterion)
    simulator_key = (
        "userSimulatorConfig"
        if "user_simulator_config" not in payload
        else "user_simulator_config"
    )
    simulator = payload.get(simulator_key)
    if simulator is None and _suite_uses_user_simulator(suite):
        # ADK creates a default simulator outside EvalConfig when a scenario
        # omits this block, so make that implicit model choice explicit here.
        simulator = {"type": "llm_backed"}
        payload[simulator_key] = simulator
    if isinstance(simulator, dict):
        if "model" not in simulator:
            simulator["model"] = _openai_model_name_from_environment()
    return payload


def _suite_uses_user_simulator(suite: EvalSuite) -> bool:
    """Detect scenario suites whose omitted simulator would otherwise use ADK defaults."""

    for path in suite.eval_sets:
        payload = json.loads(path.read_text(encoding="utf-8"))
        cases = payload.get("evalCases", payload.get("eval_cases", []))
        for case in cases:
            if any(
                case.get(key) is not None
                for key in ("conversationScenario", "conversation_scenario")
            ):
                return True
    return False


def _uses_judge_model(name: str, criterion: Any) -> bool:
    """Identify judge criteria without restricting ADK's metric registry."""

    return name in _LLM_JUDGE_METRICS or (
        isinstance(criterion, dict)
        and any(
            key in criterion for key in ("judgeModelOptions", "judge_model_options")
        )
    )


def eval_config(suite: EvalSuite, trajectory: str) -> Any:
    """Load criteria with shared model defaults and the selected trajectory."""

    require_eval_trajectory(trajectory)
    from google.adk.evaluation.eval_config import EvalConfig
    from google.adk.evaluation.eval_config import get_evaluation_criteria_or_default

    payload = _eval_config_payload(suite)
    if payload is None:
        # ADK returns a shared default object; normalize a fresh payload so one
        # request's trajectory or model settings cannot leak into another.
        payload = get_evaluation_criteria_or_default(None).model_dump(
            by_alias=True, exclude_none=True
        )
    config = EvalConfig.model_validate(_apply_openai_eval_model_defaults(payload, suite))
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
    """Expose isolated native plugins with managed identity and error reporting."""

    package_name = f"_harnest_adk_eval_{uuid.uuid4().hex}"
    module_name = f"{package_name}.agent"
    package = ModuleType(package_name)
    package.__path__ = []
    module = ModuleType(module_name)
    module.root_agent = application.target
    sys.modules[package_name] = package
    sys.modules[module_name] = module
    try:
        with prepared_adk_eval_app(application.native_app) as app:
            module.app = app
            module.root_agent = app.root_agent if app is not None else application.target
            yield module_name
    finally:
        sys.modules.pop(module_name, None)
        sys.modules.pop(package_name, None)


@contextmanager
def prepared_adk_eval_app(app: Any) -> Iterator[Any]:
    """Share CLI/playground context setup without mutating the live native app."""
    if app is None:
        yield None
        return
    from .context_adk import adk_agent_context_plugins
    from .eval_adk import evaluation_context_plugins
    from .eval_adk_cleanup import guarded_eval_plugins, guarded_eval_root
    from .eval_errors import evaluation_error_boundary, evaluation_error_plugin

    enter, exit_plugin = adk_agent_context_plugins(app.root_agent.name)
    eval_enter, eval_exit = evaluation_context_plugins()
    observer = evaluation_error_plugin()

    async def close_scope() -> None:
        """Unwind identity before capabilities when legacy ADK skips callbacks."""
        try:
            await exit_plugin.after_run_callback(invocation_context=None)
        finally:
            await eval_exit._close()
    # Capability setup precedes authored callbacks; identity and resource
    # cleanup run last so after/error callbacks can still use session identity.
    plugins = [
        customer_facing_eval_output_plugin(), eval_enter, enter, observer,
        *list(app.plugins), exit_plugin, eval_exit,
    ]
    with evaluation_error_boundary(observer):
        root = guarded_eval_root(app.root_agent, close_scope)
        plugins = guarded_eval_plugins(app.root_agent, plugins, close_scope)
        yield app.model_copy(update={"plugins": plugins, "root_agent": root})


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
