"""An evaluation package that owns a dataset, its scoring rules, and its report."""
import json

from harnest.evaluation import EvalRunnerContext, EvalRunnerResult


async def run(context: EvalRunnerContext) -> EvalRunnerResult:
    """Call the actual agent and keep provider-specific evidence in a native report."""
    case = json.loads((context.project_dir / "company-case.json").read_text())
    response = context.client.post("/responses", json={"input": case["question"]})
    response.raise_for_status()
    actual = response.json()["outputText"]
    (context.report_dir / "verdict.txt").write_text(actual)
    return EvalRunnerResult(
        "passed" if actual == context.options["expected"] else "failed",
        artifacts=("verdict.txt",),
    )
