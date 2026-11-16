"""ADK evaluation engine: run `harnest test --evals` and publish Harnest's result file.

Adapters make their agent importable for ADK's evaluator; this module scores it,
prints progress, and writes the framework-neutral EvalRunResult envelope.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    contextmanager,
    nullcontext,
)
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys
import time
from typing import Any, Iterator

from ...bundle import EvalSuite
from ...logging import get_logger
from ...testing import _EVAL_RESULT_API_VERSION, AgentTestError, _eval_progress, _write_eval_result
from .model_transport import eval_model_transports, restore_eval_model_names


_EVAL_AUDIT = get_logger("eval.audit")
_EVAL_APP_NAME = "harnest_eval"


class _EvalResultCollector:
    """Collect complete ADK case results for one Harnest eval command."""

    def __init__(self, *, framework: str, trajectory: str) -> None:
        self._framework = framework
        self._trajectory = trajectory
        self._results: dict[tuple[str, str], dict[str, Any]] = {}

    def register_eval_set(self, *, app_name: str, eval_set_id: str) -> None:
        """Retain an authored suite even when it contains no evaluable cases."""

        self._results.setdefault(
            (app_name, eval_set_id),
            {
                "appName": app_name,
                "evalSetId": eval_set_id,
                "status": "not_evaluated",
                "evalCaseResults": [],
            },
        )

    def save_eval_set_result(
        self,
        app_name: str,
        eval_set_id: str,
        eval_case_results: list[Any],
    ) -> None:
        """Retain one suite in deterministic order through ADK's manager hook."""

        # ADK supplies the logical application owner separately from the suite;
        # retain it so consumers do not need to infer result provenance.
        cases = [
            _eval_case_result_payload(result)
            for result in sorted(eval_case_results, key=lambda item: item.eval_id)
        ]
        self._results[(app_name, eval_set_id)] = {
            "appName": app_name,
            "evalSetId": eval_set_id,
            "status": _aggregate_eval_status(
                case["finalEvalStatus"] for case in cases
            ),
            "evalCaseResults": cases,
        }

    def payload(self, *, status: str) -> dict[str, Any]:
        """Build the stable command-level envelope around collected ADK results."""

        suites = sorted(self._results.values(), key=lambda item: item["evalSetId"])
        cases = [case for suite in suites for case in suite["evalCaseResults"]]
        # Infrastructure failures and scored failures must dominate partial
        # data; only a nominally successful run derives its verdict from cases.
        result_status = (
            _aggregate_eval_status(suite["status"] for suite in suites)
            if status == "passed"
            else status
        )
        return {
            "apiVersion": _EVAL_RESULT_API_VERSION,
            "kind": "EvalRunResult",
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "framework": self._framework,
            "trajectory": self._trajectory,
            "status": result_status,
            "summary": {
                "suiteCount": len(suites),
                "caseCount": len(cases),
                "passedCases": sum(
                    case["finalEvalStatus"] == "passed" for case in cases
                ),
                "failedCases": sum(
                    case["finalEvalStatus"] == "failed" for case in cases
                ),
                "notEvaluatedCases": sum(
                    case["finalEvalStatus"] == "not_evaluated" for case in cases
                ),
            },
            "evalSetResults": suites,
        }


def _aggregate_eval_status(statuses: Iterator[str]) -> str:
    """Reduce case statuses without treating missing evaluation as success."""

    values = tuple(statuses)
    # A failed case dominates a mixed suite because the command must remain
    # suitable as a CI quality gate.
    if any(value == "failed" for value in values):
        return "failed"
    # Empty and partially evaluated suites must not be reported as passing.
    if values and all(value == "passed" for value in values):
        return "passed"
    return "not_evaluated"


def _status_name(value: Any) -> str:
    """Expose ADK enum statuses as stable lowercase names instead of ordinals."""

    name = getattr(value, "name", None)
    return str(name).lower() if name is not None else str(value).lower()


def _model_json(value: Any) -> Any:
    """Serialize one ADK model with aliases and provider-safe JSON coercion."""

    if value is None:
        return None
    return restore_eval_model_names(json.loads(value.model_dump_json(by_alias=True)))


def _metric_result_payload(result: Any) -> dict[str, Any]:
    """Serialize a metric result while retaining rubric scores and criteria."""

    payload = _model_json(result)
    payload["evalStatus"] = _status_name(result.eval_status)
    return payload


def _eval_case_result_payload(result: Any) -> dict[str, Any]:
    """Preserve a complete ADK case result with readable enum statuses."""

    payload = _model_json(result)
    payload["finalEvalStatus"] = _status_name(result.final_eval_status)
    payload["overallEvalMetricResults"] = [
        _metric_result_payload(metric)
        for metric in result.overall_eval_metric_results
    ]
    invocations = []
    for invocation in result.eval_metric_result_per_invocation:
        invocation_payload = _model_json(invocation)
        invocation_payload["evalMetricResults"] = [
            _metric_result_payload(metric)
            for metric in invocation.eval_metric_results
        ]
        invocations.append(invocation_payload)
    payload["evalMetricResultPerInvocation"] = invocations
    return payload


def _publish_eval_result(
    collector: _EvalResultCollector,
    *,
    status: str,
    print_results: bool,
    result_output: Path | None,
    error: Exception | None = None,
) -> None:
    """Print and optionally persist the same complete eval result payload."""

    payload = collector.payload(status=status)
    if error is not None:
        from ...eval_errors import EvaluationExecutionError

        # Provider exceptions can echo API keys, headers, prompts, or payloads.
        # Retain only stable type-level diagnostics in the durable result.
        payload["error"] = {
            "type": type(error).__name__,
            "message": str(error) if isinstance(error, EvaluationExecutionError)
            else "evaluation infrastructure failed",
        }
    # Persist first so a broken output stream cannot discard an explicitly
    # requested result artifact after evaluation has already completed.
    if result_output is not None:
        _write_eval_result(result_output, payload)
    if print_results:
        print(json.dumps(payload, indent=2, sort_keys=True))


def _stdlib_loggers() -> list[logging.Logger]:
    values = [logging.getLogger()]
    values.extend(
        value
        for value in logging.Logger.manager.loggerDict.values()
        if isinstance(value, logging.Logger)
    )
    return values


def _restore_eval_logger(
    logger: logging.Logger,
    settings: tuple[tuple[logging.Handler, ...], int, bool, bool] | None,
) -> None:
    retained = () if settings is None else settings[0]
    for handler in tuple(logger.handlers):
        if handler in retained:
            continue
        logger.removeHandler(handler)
        handler.close()
    if settings is not None:
        _, logger.level, logger.propagate, logger.disabled = settings


def _remove_closed_stream_handlers(logger: logging.Logger) -> None:
    for handler in tuple(logger.handlers):
        stream = getattr(handler, "stream", None)
        if not bool(getattr(stream, "closed", False)):
            continue
        # In-process pytest can leave its capture handler attached after closing
        # the stream. Keeping it makes later ADK/plugin logs raise during eval.
        logger.removeHandler(handler)
        handler.close()


@contextmanager
def _isolated_eval_logging() -> Iterator[None]:
    """Remove handlers added by ADK eval dependencies when their run ends."""

    loggers = _stdlib_loggers()
    for logger in loggers:
        _remove_closed_stream_handlers(logger)
    original = {
        logger: (
            tuple(logger.handlers),
            logger.level,
            logger.propagate,
            logger.disabled,
        )
        for logger in loggers
    }
    try:
        yield
    finally:
        for logger in _stdlib_loggers():
            _restore_eval_logger(logger, original.get(logger))


def _eval_config(suite: EvalSuite, trajectory: str) -> Any:
    """Apply a named trajectory policy without replacing authored thresholds."""

    from .config import eval_config

    return eval_config(suite, trajectory)


def _eval_dependencies() -> tuple[Any, Any]:
    """Load the single evaluator dependency used by both framework adapters."""

    from ...evaluation import EvaluationError
    from .config import eval_dependencies

    try:
        return eval_dependencies()
    except EvaluationError as exc:
        raise AgentTestError(str(exc)) from exc


async def _evaluate_configured_groups(
    evaluator: Any, eval_set_class: Any, module_name: str, groups: list[tuple[EvalSuite, Any]],
    target: Any, trajectory: str, print_results: bool, collector: _EvalResultCollector,
) -> None:
    """Bind each group's judge transports and collect scored failures across groups."""
    failures = []
    offset = 0
    total = sum(len(group.eval_sets) for group, _ in groups)
    for group, config in groups:
        with eval_model_transports(target, config) as prepared:
            try:
                await _evaluate_eval_sets(
                    evaluator, eval_set_class, module_name=module_name, suite=group,
                    config=prepared, trajectory=trajectory, print_results=print_results,
                    result_collector=collector, offset=offset, total=total,
                )
            except AssertionError as exc:
                failures.append(str(exc))
        offset += len(group.eval_sets)
    if failures:
        raise AssertionError("\n\n".join(failures))


async def _evaluate_eval_sets(
    evaluator: Any,
    eval_set_class: Any,
    *,
    module_name: str,
    suite: EvalSuite,
    config: Any,
    trajectory: str,
    print_results: bool = True,
    result_collector: _EvalResultCollector | None = None,
    offset: int = 0,
    total: int | None = None,
) -> None:
    """Evaluate every suite with flushed progress, timings, and audit events."""

    scored_failures: list[str] = []
    for index, path in enumerate(suite.eval_sets, start=1):
        eval_set = eval_set_class.model_validate_json(path.read_text(encoding="utf-8"))
        if result_collector is not None:
            # ADK skips its persistence callback for an empty EvalSet, so
            # register the authored suite before evaluation to keep it visible.
            result_collector.register_eval_set(
                app_name=_EVAL_APP_NAME,
                eval_set_id=eval_set.eval_set_id,
            )
        print(f"harnest eval [{trajectory}]: {path.name}", flush=True)
        _EVAL_AUDIT.info(
            "eval.started", trigger="user", outcome="started", suite=path.name
        )
        try:
            with _eval_suite_progress(offset + index, total or len(suite.eval_sets), enabled=print_results):
                await evaluator.evaluate_eval_set(
                    agent_module=module_name,
                    eval_set=eval_set,
                    eval_config=config,
                    num_runs=1,
                    print_detailed_results=print_results,
                    app_name=_EVAL_APP_NAME if result_collector is not None else None,
                    eval_set_results_manager=result_collector,
                )
        except AssertionError as exc:
            _EVAL_AUDIT.info(
                "eval.finished",
                trigger="user",
                outcome="failed",
                suite=path.name,
            )
            # Scored failures are expected quality-gate outcomes. Continue so
            # the returned run result covers every independently authored suite.
            scored_failures.append(str(exc))
        except BaseException:
            _EVAL_AUDIT.info(
                "eval.finished",
                trigger="user",
                outcome="failed",
                suite=path.name,
            )
            raise
        else:
            _EVAL_AUDIT.info(
                "eval.finished", trigger="user", outcome="completed", suite=path.name
            )
    if scored_failures:
        raise AssertionError("\n\n".join(scored_failures))


def _run_eval_suite(
    framework: str,
    suite: EvalSuite,
    eval_target: Callable[[], AbstractAsyncContextManager[tuple[str, Any]]],
    *,
    trajectory: str,
    print_results: bool,
    result_output: Path | None,
    scope: Callable[[], AbstractContextManager[Any]] = nullcontext,
    check_config: Callable[[Any], None] | None = None,
) -> int:
    """Score one framework's agent with the built-in evaluator and publish the result.

    Adapters supply only what differs: ``eval_target`` makes the agent
    importable for the evaluator and yields its module name and the target
    whose models the evaluator borrows; ``scope`` wraps the run (import paths,
    output filters); ``check_config`` rejects settings the framework cannot run.
    """

    import asyncio

    _eval_progress("Loading evaluator and scoring configuration", enabled=print_results)
    AgentEvaluator, EvalSet = _eval_dependencies()
    groups = [(group, _eval_config(group, trajectory)) for group in suite.scoring_groups()]
    collector = _EvalResultCollector(framework=framework, trajectory=trajectory)
    if check_config is not None:
        for _group, config in groups:
            check_config(config)

    async def evaluate_all() -> None:
        """Keep borrowed model transports within the adapter's target lifetime."""
        async with eval_target() as (module_name, target):
            await _evaluate_configured_groups(
                AgentEvaluator, EvalSet, module_name, groups, target,
                trajectory, print_results, collector,
            )

    def publish(status: str, error: Exception | None = None) -> None:
        """Publish accumulated results with the suite's final status."""
        _publish_eval_result(
            collector,
            status=status,
            print_results=print_results,
            result_output=result_output,
            error=error,
        )

    try:
        with _isolated_eval_logging(), scope():
            asyncio.run(evaluate_all())
    except AssertionError as exc:
        print(str(exc), file=sys.stderr)
        publish("failed")
        return 1
    except Exception as exc:
        # Preserve suites completed before an infrastructure failure so CI can
        # distinguish an execution error from a scored eval failure.
        publish("error", exc)
        raise
    publish("passed")
    return 0


@contextmanager
def _eval_suite_progress(index: int, total: int, *, enabled: bool) -> Iterator[None]:
    """Time the native inference/scoring boundary without claiming case completion."""

    label = f"Suite {index}/{total}"
    _eval_progress(f"{label}: running agent and scoring responses", enabled=enabled)
    started = time.monotonic()
    outcome = "stopped"
    try:
        yield
        outcome = "completed"
    except AssertionError:
        outcome = "failed"
        raise
    finally:
        _eval_progress(
            f"{label} {outcome} ({time.monotonic() - started:.1f}s)", enabled=enabled,
        )
