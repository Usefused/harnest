"""Run both scoring APIs through the real CLI and an HTTP metric service."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest

ROOT = Path(__file__).resolve().parents[2]

SCORERS = '''from harnest.evaluation import MetricContext, MetricScore, metric
import httpx
import os
from google.adk.evaluation.eval_metrics import EvalStatus
from google.adk.evaluation.evaluator import EvaluationResult, PerInvocationResult

@metric
async def hosted(context: MetricContext):
    """Adapt an independently authenticated team scoring service."""
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.post(os.environ["TEST_METRIC_URL"], json={
            "actual": [turn.model_dump(mode="json", by_alias=True) for turn in context.actual],
            "expected": [turn.model_dump(mode="json", by_alias=True) for turn in context.expected],
        }, headers={"Authorization": "Bearer test-metric-token"})
        response.raise_for_status()
    value = response.json()
    if isinstance(value["score"], list):
        return [MetricScore(score, value["evidence"]) for score in value["score"]]
    return MetricScore(value["score"], value["evidence"])

def native(metric, actual, expected, scenario):
    """Keep native ADK custom metric registration working alongside Harnest."""
    return EvaluationResult(overall_score=1.0, overall_eval_status=EvalStatus.PASSED,
        per_invocation_results=[PerInvocationResult(actual_invocation=turn,
            expected_invocation=expected[index], score=1.0,
            eval_status=EvalStatus.PASSED) for index, turn in enumerate(actual)])
'''


class MetricServiceHandler(BaseHTTPRequestHandler):
    """Provide a real network boundary without relying on an external service."""

    def do_POST(self):
        """Capture the submitted evidence and return the configured service verdict."""
        self.server.payloads.append(
            json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        )
        self.server.auth.append(self.headers.get("Authorization"))
        body = json.dumps(
            {"score": self.server.score, "evidence": "Team service evidence"}
        ).encode()
        self.send_response(self.server.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        """Keep request logs out of test output."""


class CustomMetricServiceTests(unittest.TestCase):
    """Compile portable agents and exercise actual CLI exit codes and JSON reports."""

    def test_hosted_and_native_metrics_share_cli_for_both_frameworks(self):
        """Pass, fail, unscored, and unavailable services retain consistent CI outcomes."""
        with ThreadingHTTPServer(("127.0.0.1", 0), MetricServiceHandler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for framework in ("adk", "langgraph"):
                    for score, status, should_pass in (
                        (0.9, 200, True),
                        (0.2, 200, False),
                        (None, 200, False),
                        (None, 503, False),
                        ([0.6, 1.0], 200, True),
                        ([0.0, 1.0], 200, False),
                    ):
                        with self.subTest(
                            framework=framework, score=score, status=status
                        ):
                            server.score, server.status = score, status
                            server.payloads, server.auth = [], []
                            self._run_case(framework, server, should_pass)
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def _run_case(self, framework, server, should_pass):
        """Invoke the production Python CLI with native and wrapped metrics together."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "agent"
            _write_agent(root)
            output = Path(directory) / "result.json"
            env = dict(
                os.environ,
                PYTHONPATH=str(ROOT / "src"),
                TEST_METRIC_URL=f"http://127.0.0.1:{server.server_port}/score",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "harnest.cli",
                    "test",
                    str(root),
                    "--framework",
                    framework,
                    "--evals",
                    "--no-output",
                    "--eval-output",
                    str(output),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=90,
            )
            self.assertTrue(output.exists(), result.stdout + result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(result.returncode == 0, should_pass, report)
            self.assertEqual(report["status"] == "passed", should_pass, report)
            self.assertEqual(len(server.payloads), 1)
            self.assertEqual(server.auth, ["Bearer test-metric-token"])
            self.assertEqual(len(server.payloads[0]["actual"]), 2)
            self.assertEqual(len(server.payloads[0]["expected"]), 2)
            if server.status == 200:
                serialized = json.dumps(report)
                self.assertIn("Team service evidence", serialized)
                self.assertIn('"native"', serialized)
                self.assertIn('"hosted"', serialized)


def _write_agent(root):
    """Create one portable, deterministic graph so only the scorer needs a service."""
    files = {
        "agent.py": '''from harnest.graph import START, Edge, Event, Graph

def respond(value):
    """Return deterministic public evidence for the scoring service."""
    return Event(message="answer")

root_agent = Graph(name="root", nodes={"respond": respond}, edges=(Edge(START, "respond"),))
''',
        "instructions.md": "Respond clearly.",
        "agent-card.yaml": "name: Metric test\ndescription: Hosted and native scoring\n",
        "lib/metrics.py": SCORERS,
        "lifecycle/storage.py": '''from harnest import lifecycle
from harnest.store import MemoryStore
store = MemoryStore()
@lifecycle.storage.sessions
def sessions():
    """Use isolated test sessions."""
    return store
@lifecycle.storage.checkpoints
def checkpoints():
    """Use the same owner for native checkpoints."""
    return store
''',
        "evals/test_config.json": json.dumps(
            {
                "criteria": {"hosted": 0.8, "native": 1.0},
                "customMetrics": {
                    name: {"codeConfig": {"name": f"harnest.lib.metrics.{name}"}}
                    for name in ("hosted", "native")
                },
            }
        ),
        "evals/service.evalset.json": json.dumps(
            {
                "eval_set_id": "service",
                "eval_cases": [
                    {
                        "evalId": "two-turns",
                        "conversation": [
                            {
                                "userContent": {
                                    "role": "user",
                                    "parts": [{"text": prompt}],
                                },
                                "finalResponse": {
                                    "role": "model",
                                    "parts": [{"text": "answer"}],
                                },
                            }
                            for prompt in ("first question", "second question")
                        ],
                    }
                ],
            }
        ),
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
