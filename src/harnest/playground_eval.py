"""Local evaluation catalog and execution service for the playground."""

from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
import time
from typing import Any, Mapping

from .bundle import EvalSuite, discover_evals
from .evaluation import (
    EvaluationError,
    adk_eval_agent_module,
    eval_config,
    eval_dependencies,
    require_eval_trajectory,
    supported_metric_names,
)
from .logging import get_logger


_EVAL_AUDIT = get_logger("eval.audit")


@dataclass(frozen=True, slots=True)
class _EvalRow:
    eval_id: str
    metric: str
    threshold: float | None
    score: float | None
    status: str
    prompt: str
    expected_response: str
    actual_response: str
    expected_tool_calls: str
    actual_tool_calls: str


class PlaygroundEvalService:
    """Discover and execute the compiled artifact's validated local eval suites."""

    def __init__(
        self,
        artifact: str | Path,
        application: Any,
        driver: Any,
    ) -> None:
        self._artifact = Path(artifact).resolve()
        self._application = application
        self._driver = driver
        self._suite = discover_evals(self._artifact / "source" / "agent.py")
        self._paths = {
            path.name[: -len(".evalset.json")]: path
            for path in self._suite.eval_sets
        }
        # ADK registers custom metrics globally during a run, so one server must
        # not overlap configs even when several browser requests arrive together.
        self._run_lock = asyncio.Lock()

    def catalog(self) -> dict[str, Any]:
        """Return browser-safe suite and metric metadata without source paths."""

        metrics = _configured_metrics(self._suite)
        return {
            "framework": self._application.framework,
            "trajectories": ["business", "strict"],
            "suites": [_suite_summary(path) for path in self._suite.eval_sets],
            "metrics": metrics,
            "supportedMetrics": list(supported_metric_names()),
        }

    async def run(self, suite_id: str, trajectory: str) -> dict[str, Any]:
        """Run one selected suite and return structured case and metric results."""

        require_eval_trajectory(trajectory)
        path = self._paths.get(suite_id)
        if path is None:
            raise KeyError(suite_id)
        async with self._run_lock:
            return await self._run_locked(path, trajectory)

    async def _run_locked(self, path: Path, trajectory: str) -> dict[str, Any]:
        """Own evaluator registration and temporary output for one complete run."""

        evaluator, eval_set_class = eval_dependencies()
        eval_set = eval_set_class.model_validate_json(path.read_text(encoding="utf-8"))
        selected_suite = EvalSuite((path,), self._suite.config)
        config = eval_config(selected_suite, trajectory)
        started = time.monotonic()
        _EVAL_AUDIT.info(
            "eval.started", trigger="user", outcome="started", suite=path.name
        )
        failure = None
        try:
            with tempfile.TemporaryDirectory(prefix="harnest-playground-eval-") as tmp:
                output = Path(tmp) / "results.csv"
                try:
                    with self._agent_module() as module_name:
                        await evaluator.evaluate_eval_set(
                            agent_module=module_name,
                            eval_set=eval_set,
                            eval_config=config,
                            num_runs=1,
                            print_detailed_results=False,
                            output_file=str(output),
                        )
                except AssertionError as exc:
                    # Metric failures are a successful evaluation run with a
                    # failed outcome, not an HTTP infrastructure failure.
                    failure = str(exc)
                rows = _read_rows(output)
        except BaseException:
            _EVAL_AUDIT.info(
                "eval.finished", trigger="user", outcome="failed", suite=path.name
            )
            raise
        status = "failed" if failure or _has_failed_rows(rows) else "passed"
        _EVAL_AUDIT.info(
            "eval.finished", trigger="user", outcome=status, suite=path.name
        )
        return _run_result(
            eval_set,
            rows,
            suite_id=path.name[: -len(".evalset.json")],
            trajectory=trajectory,
            duration_ms=round((time.monotonic() - started) * 1000),
            failure=failure,
        )

    def _agent_module(self) -> Any:
        """Select native ADK fidelity or the active neutral LangGraph driver."""

        if self._application.framework == "adk":
            return adk_eval_agent_module(self._application)
        from .eval_langgraph import runtime_eval_agent_module

        if self._driver is None:
            raise EvaluationError("LangGraph playground evals require a runtime driver")
        return runtime_eval_agent_module(self._driver)


def _suite_summary(path: Path) -> dict[str, Any]:
    """Project authored display metadata from one already validated EvalSet."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("eval_cases", payload.get("evalCases", []))
    return {
        "id": payload["eval_set_id"],
        "name": payload.get("name") or payload["eval_set_id"],
        "description": payload.get("description") or "",
        "caseCount": len(cases),
        "cases": [
            {
                "id": case.get("eval_id", case.get("evalId")),
                "name": case.get("name") or case.get("eval_id", case.get("evalId")),
            }
            for case in cases
        ],
    }


def _configured_metrics(suite: EvalSuite) -> list[dict[str, Any]]:
    """Describe effective authored/default criteria and custom registrations."""

    if not suite.eval_sets:
        return []
    config = eval_config(suite, "business")
    custom = set((getattr(config, "custom_metrics", None) or {}).keys())
    return [
        {
            "name": name,
            "threshold": _criterion_threshold(criterion),
            "custom": name in custom,
        }
        for name, criterion in sorted(config.criteria.items())
    ]


def _criterion_threshold(criterion: Any) -> float | None:
    """Normalize both legacy float criteria and typed ADK criterion objects."""

    value = criterion if isinstance(criterion, (int, float)) else criterion.threshold
    return float(value) if value is not None else None


def _read_rows(path: Path) -> tuple[_EvalRow, ...]:
    """Decode the evaluator's public CSV export into stable playground fields."""

    if not path.is_file() or path.stat().st_size == 0:
        return ()
    with path.open(encoding="utf-8", newline="") as stream:
        return tuple(_eval_row(row) for row in csv.DictReader(stream))


def _eval_row(row: Mapping[str, str]) -> _EvalRow:
    """Normalize one public ADK evaluator row without retaining CSV quirks."""

    return _EvalRow(
        eval_id=row.get("eval_id", ""),
        metric=row.get("metric_name", ""),
        threshold=_optional_float(row.get("threshold")),
        score=_optional_float(row.get("score")),
        status=row.get("eval_status", "UNKNOWN").lower(),
        prompt=row.get("prompt", ""),
        expected_response=row.get("expected_response", ""),
        actual_response=row.get("actual_response", ""),
        expected_tool_calls=row.get("expected_tool_calls", ""),
        actual_tool_calls=row.get("actual_tool_calls", ""),
    )


def _optional_float(value: str | None) -> float | None:
    """Treat empty or non-numeric evaluator cells as unavailable scores."""

    try:
        return float(value) if value not in {None, ""} else None
    except ValueError:
        return None


def _has_failed_rows(rows: tuple[_EvalRow, ...]) -> bool:
    return any(row.status != "passed" for row in rows)


def _run_result(
    eval_set: Any,
    rows: tuple[_EvalRow, ...],
    *,
    suite_id: str,
    trajectory: str,
    duration_ms: int,
    failure: str | None,
) -> dict[str, Any]:
    """Aggregate invocation rows while retaining drill-down response evidence."""

    case_ids = [case.eval_id for case in eval_set.eval_cases]
    cases = [_case_result(case_id, rows) for case_id in case_ids]
    metrics = [_metric_result(name, rows) for name in sorted({row.metric for row in rows})]
    passed_cases = sum(case["status"] == "passed" for case in cases)
    status = "failed" if failure or passed_cases != len(cases) else "passed"
    return {
        "suiteId": suite_id,
        "trajectory": trajectory,
        "status": status,
        "durationMs": duration_ms,
        "failure": failure,
        "summary": {
            "caseCount": len(cases),
            "passedCases": passed_cases,
            "failedCases": len(cases) - passed_cases,
            "metricCount": len(metrics),
        },
        "metrics": metrics,
        "cases": cases,
    }


def _case_result(case_id: str, rows: tuple[_EvalRow, ...]) -> dict[str, Any]:
    """Group all invocation evidence for one authored evaluation case."""

    selected = [row for row in rows if row.eval_id == case_id]
    status = "passed" if selected and not _has_failed_rows(tuple(selected)) else "failed"
    return {
        "id": case_id,
        "status": status,
        "results": [_row_payload(row) for row in selected],
    }


def _metric_result(name: str, rows: tuple[_EvalRow, ...]) -> dict[str, Any]:
    """Summarize one metric across cases without hiding invocation-level rows."""

    selected = [row for row in rows if row.metric == name]
    scores = [row.score for row in selected if row.score is not None]
    return {
        "name": name,
        "status": "passed" if not _has_failed_rows(tuple(selected)) else "failed",
        "score": sum(scores) / len(scores) if scores else None,
        "threshold": selected[0].threshold if selected else None,
        "resultCount": len(selected),
    }


def _row_payload(row: _EvalRow) -> dict[str, Any]:
    """Return one JSON-safe invocation result for the browser drill-down."""

    return {
        "metric": row.metric,
        "status": row.status,
        "score": row.score,
        "threshold": row.threshold,
        "prompt": row.prompt,
        "expectedResponse": row.expected_response,
        "actualResponse": row.actual_response,
        "expectedToolCalls": row.expected_tool_calls,
        "actualToolCalls": row.actual_tool_calls,
    }


__all__ = ["PlaygroundEvalService"]
