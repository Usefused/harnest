"""Keep the runnable Jev example compatible with both managed framework adapters."""

import os
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from harnest.bundle import compile_artifact
from harnest.runtime import create_fastapi_app
from harnest.project_lock import resolve_project_lock


EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "jev-triage"


class JevExampleTests(unittest.TestCase):
    def _source(self, directory: str, framework: str) -> Path:
        """Resolve the selected framework in a copy without changing the authored lock."""
        source = Path(directory) / "source"
        shutil.copytree(EXAMPLE, source, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".harnest", ".venv"))
        (source / "harnest.lock").unlink()
        resolve_project_lock(source, framework)
        return source

    def _serve(self, framework: str) -> None:
        """Compile without provider credentials and serve the explicitly selected fixture."""
        with tempfile.TemporaryDirectory() as directory:
            source = self._source(directory, framework)
            artifact = Path(directory) / "agent"
            with patch.dict(os.environ, {"JEV_TRIAGE_OFFLINE": "false", "TYPESAFE_API_KEY": "", "TYPESAFE_AI_KEY": ""}):
                compile_artifact(source, artifact, framework=framework)
            with patch.dict(os.environ, {"JEV_TRIAGE_OFFLINE": "true", "TYPESAFE_API_KEY": "", "TYPESAFE_AI_KEY": ""}):
                self._assert_response(artifact)

    def _assert_response(self, artifact: Path) -> None:
        """Validate the API's structured recommendation and honest offline labeling."""
        with TestClient(create_fastapi_app(artifact)) as client:
            session = client.post("/sessions", json={})
            self.assertEqual(session.status_code, 201, session.text)
            response = client.post("/responses", json={
                "sessionId": session.json()["id"], "input": "I was charged twice.",
            })
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()["result"]
            self.assertEqual(set(result), {"reply"})
            self.assertNotIn("decision_result", response.text)
            self.assertIn("billing", response.json()["outputText"])
            self.assertIn("Offline fixture", result["reply"])

    def _serve_llm(self, framework: str) -> None:
        """Exercise the real LLM node with a fixture decision and mocked model transport."""
        import litellm

        complete = litellm.acompletion
        calls = []
        reply = "Billing can review the duplicate charge. Please have your order reference ready."

        async def mock_completion(**kwargs):
            """Record native model inputs while replacing every outbound completion."""
            calls.append(kwargs)
            return await complete(**kwargs, mock_response=reply)

        environment = {"JEV_TRIAGE_OFFLINE": "false", "JEV_LLM_MODEL": "openai/test",
                       "JEV_LLM_API_BASE": "https://models.example.invalid/v1", "JEV_LLM_API_KEY": "test-only"}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, environment):
            source = self._source(directory, framework)
            # Keep the actual live reply node; only the Jev resource is replaced.
            (source / "lifecycle/decisions.py").write_text(
                "from contextlib import contextmanager\n"
                "from harnest import context, lifecycle\n"
                "from harnest.lib.decision_resources import offline_registry\n"
                "@lifecycle.resource\n@context.provider('decisions')\n@contextmanager\n"
                "def decisions():\n    yield offline_registry()\n"
            )
            artifact = Path(directory) / "agent"
            compile_artifact(source, artifact, framework=framework)
            with patch("litellm.acompletion", new=mock_completion), patch("google.adk.models.lite_llm.acompletion", new=mock_completion):
                with TestClient(create_fastapi_app(artifact)) as client:
                    session = client.post("/sessions", json={}).json()["id"]
                    response = client.post("/responses", json={"sessionId": session, "input": "I was charged twice."})
                    self.assertEqual(response.status_code, 200, response.text)
                    result = response.json()["result"]
                    transcript = client.get(f"/sessions/{session}/messages").json()["messages"]
                    user_turns = [item for item in transcript if item["role"] == "user"]
                    self.assertEqual(len(user_turns), 1, user_turns)
                    self.assertNotIn("jev_triage_decision", json.dumps(transcript))

            self.assertEqual(result, {"reply": reply})
            self.assertNotIn("decision_result", response.text)
            self.assertEqual(response.json()["outputText"].count(reply), 1)
            self.assertEqual(len(calls), 1)
            prompt = json.dumps(calls[0]["messages"])
            self.assertIn("I was charged twice.", prompt)
            self.assertIn("billing", prompt)
            self.assertIn("Do not change or reclassify", prompt)

    def test_adk_llm_receives_ticket_and_decision(self):
        self._serve_llm("adk")

    def test_langgraph_llm_receives_ticket_and_decision(self):
        self._serve_llm("langgraph")

    def test_adk_example(self):
        self._serve("adk")

    def test_langgraph_example(self):
        self._serve("langgraph")
