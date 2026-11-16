"""Local evaluation catalog and execution service for the playground."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
from typing import Any

from .bundle import EvalSuite, discover_evals
from .evaluation import require_eval_trajectory
from .evaluators import EvalResultRow
from .logging import get_logger


_EVAL_AUDIT = get_logger("eval.audit")


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
        self._suite = discover_evals(
            self._artifact / "source" / "agent.py", framework=application.framework
        )
        self._paths = {
            path.name[: -len(".evalset.json")]: path
            for path in self._suite.eval_sets
        }
        # ADK registers custom metrics globally during a run, so one server must
        # not overlap configs even when several browser requests arrive together.
        self._run_lock = asyncio.Lock()

    def catalog(self) -> dict[str, Any]:
        """Return browser-safe suite and metric metadata without source paths."""

        return {
            "framework": self._application.framework,
            "trajectories": ["business", "strict"],
            "suites": [_suite_summary(path) for path in self._suite.eval_sets],
            "metrics": _catalog_metrics(self._evaluator(), self._suite),
            "supportedMetrics": list(self._evaluator().supported_metric_names()),
        }

    async def run(self, suite_id: str, trajectory: str) -> dict[str, Any]:
        """Run one selected suite and return structured case and metric results."""

        require_eval_trajectory(trajectory)
        path = self._paths.get(suite_id)
        if path is None:
            raise KeyError(suite_id)
        async with self._run_lock:
            from .frameworks.registry import get_framework

            adapter = get_framework(self._application.framework)
            async with adapter.evaluation_runtime(self._application, self._driver):
                return await self._run_locked(path, trajectory)

    async def _run_locked(self, path: Path, trajectory: str) -> dict[str, Any]:
        """Own evaluator registration and temporary output for one complete run."""

        evaluator = self._evaluator()
        prepared = evaluator.prepare_playground_set(path, self._suite.for_set(path), trajectory)
        started = time.monotonic()
        _EVAL_AUDIT.info(
            "eval.started", trigger="user", outcome="started", suite=path.name
        )
        try:
            rows, failure = await evaluator.run_playground_set(
                prepared, agent_module=self._agent_module, target=self._application.target,
            )
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
            prepared.case_ids,
            rows,
            suite_id=path.name[: -len(".evalset.json")],
            trajectory=trajectory,
            duration_ms=round((time.monotonic() - started) * 1000),
            failure=failure,
        )

    def _evaluator(self) -> Any:
        """Use the evaluation engine the application's framework adapter names."""

        from .frameworks.registry import get_framework

        return get_framework(self._application.framework).evaluator()

    def _agent_module(self) -> Any:
        """Select native ADK fidelity or the active neutral LangGraph driver."""

        from .frameworks.registry import get_framework

        adapter = get_framework(self._application.framework)
        return adapter.playground_eval_agent_module(self._application, self._driver)


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


def _catalog_metrics(evaluator: Any, suite: EvalSuite) -> list[dict[str, Any]]:
    """Summarize distinct metrics without reporting a set-specific threshold as global."""
    metrics: dict[str, dict[str, Any]] = {}
    for group in suite.scoring_groups():
        for row in evaluator.configured_metrics(group):
            summary = metrics.setdefault(row["name"], row.copy())
            if summary["threshold"] != row["threshold"]:
                summary["threshold"] = None
            summary["custom"] |= row["custom"]
    return [metrics[name] for name in sorted(metrics)]


def _has_failed_rows(rows: tuple[EvalResultRow, ...]) -> bool:
    """ADK efficiency observations do not decide whether a scored criterion passed."""
    return any(row.status not in {"passed", "informational"} for row in rows)


def _run_result(
    case_ids: tuple[str, ...],
    rows: tuple[EvalResultRow, ...],
    *,
    suite_id: str,
    trajectory: str,
    duration_ms: int,
    failure: str | None,
) -> dict[str, Any]:
    """Aggregate invocation rows while retaining drill-down response evidence."""

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


def _case_result(case_id: str, rows: tuple[EvalResultRow, ...]) -> dict[str, Any]:
    """Group all invocation evidence for one authored evaluation case."""

    selected = [row for row in rows if row.eval_id == case_id]
    status = "passed" if selected and not _has_failed_rows(tuple(selected)) else "failed"
    return {
        "id": case_id,
        "status": status,
        "results": [_row_payload(row) for row in selected],
    }


def _metric_result(name: str, rows: tuple[EvalResultRow, ...]) -> dict[str, Any]:
    """Summarize one metric across cases without hiding invocation-level rows."""

    selected = [row for row in rows if row.metric == name]
    scores = [row.score for row in selected if row.score is not None]
    status = "failed" if _has_failed_rows(tuple(selected)) else "passed"
    if {row.status for row in selected} == {"informational"}:
        status = "informational"
    return {
        "name": name,
        "status": status,
        "score": sum(scores) / len(scores) if scores else None,
        "threshold": selected[0].threshold if selected else None,
        "resultCount": len(selected),
    }


def _row_payload(row: EvalResultRow) -> dict[str, Any]:
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
