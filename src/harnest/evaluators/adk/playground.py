"""ADK evaluation engine: run one eval set for the Playground and read its result rows."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
import csv
from dataclasses import dataclass
from pathlib import Path
import tempfile
from typing import Any, Mapping

from ...bundle import EvalSuite
from .. import EvalResultRow
from .config import eval_config, eval_dependencies
from .model_transport import eval_model_transports


@dataclass(frozen=True, slots=True)
class PreparedPlaygroundSet:
    """One validated eval set and its scoring configuration, ready to run."""

    eval_set: Any
    config: Any
    evaluator: Any

    @property
    def case_ids(self) -> tuple[str, ...]:
        """Authored case IDs in file order."""
        return tuple(case.eval_id for case in self.eval_set.eval_cases)


def prepare_playground_set(path: Path, suite: EvalSuite, trajectory: str) -> PreparedPlaygroundSet:
    """Load the evaluator, the eval set, and its criteria before the run starts."""

    evaluator, eval_set_class = eval_dependencies()
    eval_set = eval_set_class.model_validate_json(path.read_text(encoding="utf-8"))
    return PreparedPlaygroundSet(eval_set, eval_config(suite, trajectory), evaluator)


async def run_playground_set(
    prepared: PreparedPlaygroundSet,
    *,
    agent_module: Callable[[], AbstractContextManager[str]],
    target: Any,
) -> tuple[tuple[EvalResultRow, ...], str | None]:
    """Run the set and return its result rows and any scored-failure message."""

    failure = None
    with tempfile.TemporaryDirectory(prefix="harnest-playground-eval-") as tmp:
        output = Path(tmp) / "results.csv"
        try:
            with agent_module() as module_name, eval_model_transports(
                target, prepared.config
            ) as config:
                await prepared.evaluator.evaluate_eval_set(
                    agent_module=module_name,
                    eval_set=prepared.eval_set,
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
    return rows, failure


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

def _read_rows(path: Path) -> tuple[EvalResultRow, ...]:
    """Decode the evaluator's public CSV export into stable playground fields."""

    if not path.is_file() or path.stat().st_size == 0:
        return ()
    with path.open(encoding="utf-8", newline="") as stream:
        return tuple(_eval_row(row) for row in csv.DictReader(stream))

def _eval_row(row: Mapping[str, str]) -> EvalResultRow:
    """Normalize one public ADK evaluator row without retaining CSV quirks."""

    return EvalResultRow(
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
