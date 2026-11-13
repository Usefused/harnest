"""Exercise installed Python runner packages through the Harnest test entrypoint."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from jsonschema import Draft202012Validator
import yaml

from test_eval_custom_service import ROOT, _write_agent


class EvaluationRunnerTests(unittest.TestCase):
    def test_external_wheel_alone_and_alongside_adk(self):
        """Compare provider-owned data, ADK's native report, verdicts, and error isolation."""
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            self._install_package(work)
            agent = work / "agent"
            _write_agent(agent)
            # Company-only runs must work without an ADK dataset or ADK metric configuration.
            (agent / "evals").rename(agent / "saved-adk-suite")
            (agent / "company-case.json").write_text('{"question":"hello"}')
            company = {"name": "company", "entrypoint": "company_evals:run", "options": {"expected": "answer"}}
            code, report = self._run(work, agent, [company])
            self.assertEqual((code, report["status"]), (0, "passed"))
            native = work / report["runners"][0]["artifacts"][0]
            self.assertEqual(native.read_text(), "answer")

            (agent / "saved-adk-suite").rename(agent / "evals")
            (agent / "evals/test_config.json").write_text('{"criteria":{"response_match_score":0.5}}')
            company["options"]["expected"] = "different answer"
            code, report = self._run(work, agent, [company, {"name": "adk"}])
            self.assertEqual((code, report["status"]), (1, "failed"))
            self.assertEqual([r["status"] for r in report["runners"]], ["failed", "passed"])
            adk_report = json.loads((work / report["runners"][1]["artifacts"][0]).read_text())
            self.assertEqual(adk_report["kind"], "EvalRunResult")

            missing = {"name": "unavailable", "entrypoint": "package_does_not_exist:run"}
            code, report = self._run(work, agent, [missing, company])
            self.assertEqual((code, report["status"]), (2, "error"))
            self.assertEqual([r["status"] for r in report["runners"]], ["error", "failed"])
            self.assertEqual(native.read_text(), "answer", "later runs must retain earlier artifacts")

    def _install_package(self, work):
        """Install the fixture as a wheel into an isolated import target without network I/O."""
        subprocess.run([sys.executable, "-m", "build", "--no-isolation", "--wheel",
                        "--outdir", str(work), str(ROOT / "tests/fixtures/eval-runner")], check=True, capture_output=True)
        subprocess.run([sys.executable, "-m", "pip", "install", "--no-index", "--no-deps",
                        "--target", str(work / "installed"), str(next(work.glob("*.whl")))], check=True, capture_output=True)

    def _run(self, work, agent, runners):
        """Exercise compilation and invocation in a fresh consumer interpreter."""
        (agent / "config.yaml").write_text(yaml.safe_dump({"spec": {"evaluation": {"runners": runners}}}))
        output = work / "summary.json"
        output.unlink(missing_ok=True)
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(map(str, (ROOT / "src", work / "installed"))))
        result = subprocess.run([sys.executable, "-m", "harnest.cli", "test", str(agent),
                                 "--framework", "langgraph", "--evals", "--eval-output", str(output)],
                                env=env, capture_output=True, text=True, timeout=90)
        self.assertTrue(output.exists(), result.stderr)
        report = json.loads(output.read_text())
        schema = json.loads((ROOT / "schemas/eval-runner-summary.schema.json").read_text())
        Draft202012Validator(schema).validate(report)
        return result.returncode, report
