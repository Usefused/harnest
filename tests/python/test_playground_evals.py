import json
import importlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from harnest.bundle import compile_artifact
from harnest.evaluation import adk_eval_agent_module
from harnest.runtime import create_fastapi_app


class PlaygroundEvalTests(unittest.TestCase):
    def test_langgraph_playground_lists_and_runs_custom_metric(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "authored"
            artifact = Path(directory) / "compiled"
            self._write(
                root / "agent.py",
                "from harnest.graph import START, Edge, Event, Graph\n\n"
                "def respond(value):\n"
                "    return Event(message='official response')\n\n"
                "root_agent = Graph(\n"
                "    name='root', nodes={'respond': respond},\n"
                "    edges=(Edge(START, 'respond'),),\n"
                ")\n",
            )
            self._write(root / "instructions.md", "Answer clearly.\n")
            self._write(
                root / "extensions" / "sessions.py",
                "from harnest.lifecycle import lifecycle\n"
                "from harnest.session import InMemorySessionStore\n"
                "@lifecycle.session_store\n"
                "def session_store(): return InMemorySessionStore()\n",
            )
            self._write(
                root / "extensions" / "checkpoints.py",
                "from harnest.checkpoint import MemoryStore\n"
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.checkpointer\n"
                "def checkpointer(): return MemoryStore()\n",
            )
            self._write(
                root / "agent-card.yaml",
                json.dumps({"name": "Root", "description": "Eval test"}),
            )
            self._write(
                root / "lib" / "eval_metrics.py",
                "from google.adk.evaluation.eval_metrics import EvalStatus\n"
                "from google.adk.evaluation.evaluator import (\n"
                "    EvaluationResult, PerInvocationResult,\n"
                ")\n\n"
                "def response_present(metric, actual, expected, scenario):\n"
                "    del metric, scenario\n"
                "    details = [PerInvocationResult(\n"
                "        actual_invocation=item,\n"
                "        expected_invocation=expected[index],\n"
                "        score=1.0, eval_status=EvalStatus.PASSED,\n"
                "    ) for index, item in enumerate(actual)]\n"
                "    return EvaluationResult(\n"
                "        overall_score=1.0,\n"
                "        overall_eval_status=EvalStatus.PASSED,\n"
                "        per_invocation_results=details,\n"
                "    )\n",
            )
            self._write(
                root / "evals" / "portable.evalset.json",
                json.dumps(
                    {
                        "eval_set_id": "portable",
                        "name": "Portable response",
                        "eval_cases": [
                            {
                                "evalId": "responds",
                                "conversation": [
                                    {
                                        "userContent": {
                                            "role": "user",
                                            "parts": [{"text": "answer"}],
                                        },
                                        "finalResponse": {
                                            "role": "model",
                                            "parts": [{"text": "official response"}],
                                        },
                                    }
                                ],
                                "sessionInput": {
                                    "appName": "root",
                                    "userId": "eval-user",
                                    "state": {},
                                },
                            }
                        ],
                    }
                ),
            )
            self._write(
                root / "evals" / "test_config.json",
                json.dumps(
                    {
                        "criteria": {"custom_quality": 1.0},
                        "customMetrics": {
                            "custom_quality": {
                                "codeConfig": {
                                    "name": "harnest.lib.eval_metrics.response_present"
                                }
                            }
                        },
                    }
                ),
            )
            compile_artifact(root, artifact, framework="langgraph")
            app = create_fastapi_app(artifact, bind_host="testserver")

            from fastapi.testclient import TestClient

            with TestClient(app) as client:
                page = client.get("/")
                catalog = client.get("/_harnest/evals")
                run = client.post(
                    "/_harnest/evals/run",
                    json={"suiteId": "portable", "trajectory": "business"},
                )
                missing = client.post(
                    "/_harnest/evals/run",
                    json={"suiteId": "missing", "trajectory": "business"},
                )
                invalid = client.post(
                    "/_harnest/evals/run",
                    json={"suiteId": "portable", "trajectory": "loose"},
                )

        self.assertIn("Evals", page.text)
        self.assertEqual(catalog.status_code, 200, catalog.text)
        body = catalog.json()
        self.assertEqual(body["framework"], "langgraph")
        self.assertEqual(body["suites"][0]["id"], "portable")
        self.assertEqual(body["suites"][0]["caseCount"], 1)
        self.assertEqual(
            body["metrics"],
            [{"name": "custom_quality", "threshold": 1.0, "custom": True}],
        )
        self.assertTrue(body["supportedMetrics"])
        self.assertEqual(run.status_code, 200, run.text)
        result = run.json()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["summary"]["passedCases"], 1)
        self.assertEqual(result["metrics"][0]["name"], "custom_quality")
        self.assertEqual(result["cases"][0]["results"][0]["score"], 1.0)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(invalid.status_code, 400)

    def test_adk_eval_module_copies_plugins_away_from_live_app(self):
        from google.adk.agents import LlmAgent
        from google.adk.apps import App

        root = LlmAgent(name="root", model="gemini-test")
        live_app = App(name="eval_test", root_agent=root, plugins=[])
        application = SimpleNamespace(target=root, native_app=live_app)

        with adk_eval_agent_module(application) as module_name:
            module = importlib.import_module(module_name)
            self.assertIs(module.root_agent, root)
            self.assertIsNot(module.app, live_app)
            self.assertEqual(
                module.app.plugins[0].name,
                "_harnest_customer_facing_eval_output",
            )

        self.assertEqual(live_app.plugins, [])

    @staticmethod
    def _write(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
