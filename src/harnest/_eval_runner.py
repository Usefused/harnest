"""Execute a team's evaluation packages and retain their independent reports."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib import import_module
import inspect
import json
from pathlib import Path
import tempfile
from typing import Any

from fastapi.testclient import TestClient

from ._eval_runner_api import EvalRunnerContext, EvalRunnerResult
from ._eval_runner_config import RunnerSpec
from .runtime import create_fastapi_app


class _RunnerContractError(ValueError):
    """A safe configuration or result diagnostic suitable for the durable summary."""


async def _await_result(value: Awaitable[Any]) -> Any:
    """Await custom async entrypoints on a loop owned by the test command."""
    return await value


def _reports(directory: Path, artifacts: tuple[str, ...]) -> list[Path]:
    """Require every reported file to exist beneath this execution's directory."""
    paths = []
    for name in artifacts:
        if not isinstance(name, str) or Path(name).is_absolute():
            raise _RunnerContractError("artifact names must be relative paths")
        path = (directory / name).resolve()
        if not path.is_relative_to(directory) or not path.is_file():
            raise _RunnerContractError("artifact must be an existing file inside report_dir")
        paths.append(path)
    return paths


def _custom(spec: RunnerSpec, artifact: Path, project: Path, directory: Path) -> EvalRunnerResult:
    """Import only the selected entrypoint and close its HTTP application after the run."""
    module, symbol = spec.entrypoint.split(":")
    function = getattr(import_module(module), symbol)
    app = create_fastapi_app(artifact, bind_host="testserver", playground_enabled=False)
    with TestClient(app) as client:
        result = function(EvalRunnerContext(client, project, directory, spec.options))
        if inspect.isawaitable(result):
            result = asyncio.run(_await_result(result))
    if not isinstance(result, EvalRunnerResult) or result.status not in {"passed", "failed", "not_evaluated"}:
        raise _RunnerContractError("return EvalRunnerResult with a valid status")
    if not isinstance(result.artifacts, tuple):
        raise _RunnerContractError("artifacts must be a tuple of relative file names")
    return result


@dataclass
class _Execution:
    """Keep compilation inputs shared but output and failures independent for each runner."""

    artifact: Path
    project: Path
    run_dir: Path
    run_adk: Callable[[Path], int]
    report_base: Path

    def execute(self, spec: RunnerSpec) -> dict[str, Any]:
        """Catch package failures at the runner boundary so later suites still execute."""
        directory = self.run_dir / spec.name
        reports: list[Path] = []
        try:
            directory.mkdir()
            if spec.name == "adk":
                path = directory / "result.json"
                try:
                    self.run_adk(path)
                finally:
                    if path.is_file():
                        reports.append(path)
                status = json.loads(path.read_text(encoding="utf-8"))["status"]
            else:
                result = _custom(spec, self.artifact, self.project, directory)
                status = result.status
                reports = _reports(directory, result.artifacts)
            row = {"name": spec.name, "status": status}
        except Exception as exc:
            # A third-party exception may contain credentials. Preserve its class, never its payload.
            row = {"name": spec.name, "status": "error", "error": type(exc).__name__}
            if isinstance(exc, _RunnerContractError):
                row["message"] = str(exc)
        return {**row, "artifacts": [str(path.relative_to(self.report_base)) for path in reports]}


def run_configured(
    runners: list[RunnerSpec], artifact: Path, project: Path, output: Path | None,
    run_adk: Callable[[Path], int], publish: Callable[[Path, dict[str, Any]], None],
    *, quiet: bool,
) -> int:
    """Write the combined verdict atomically, referring only to reports from this invocation."""
    parent = output.expanduser().parent.resolve() if output else project / ".harnest" / "eval-results"
    parent.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=parent))
    summary = parent / output.name if output else run_dir / "summary.json"
    execution = _Execution(artifact, project, run_dir, run_adk, summary.parent)
    rows = [execution.execute(spec) for spec in runners]
    statuses = {row["status"] for row in rows}
    verdict = next((v for v in ("error", "failed", "not_evaluated") if v in statuses), "passed")
    payload = {"apiVersion": "harnest.dev/v1alpha1", "kind": "EvalRunnerSummary",
               "status": verdict, "runners": rows}
    publish(summary, payload)
    if not quiet:
        print(json.dumps(payload, indent=2))
        print(f"Evaluation summary: {summary}")
    return 2 if verdict == "error" else int(verdict != "passed")
