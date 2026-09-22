"""Compile and serve decision resources across ADK, LangGraph and extensions."""

import json
from pathlib import Path
import tempfile
import textwrap
import unittest

from fastapi.testclient import TestClient

from harnest.bundle import compile_artifact
from harnest.runtime import create_fastapi_app
from _session_store_fixture import write_session_store


def write(path: Path, content: str) -> None:
    """Write an isolated authored resource in a temporary application."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def agent(root: Path) -> None:
    """Use an asynchronous managed graph to exercise decisions without an LLM."""
    write(root / "agent.py", '''
        from harnest import context
        from harnest.graph import START, Edge, Event, Graph

        async def decide(value):
            result = await context.decisions.evaluate("triage", {"message": value})
            return Event(output=result.outcome.route, message=result.outcome.route)

        root_agent = Graph(name="triage_agent", nodes={"decide": decide},
                           edges=(Edge(START, "decide"),))
    ''')
    write(root / "instructions.md", "Route the request using the authored decision.")
    write(root / "agent-card.yaml", "name: Triage\ndescription: Test decisions\nversion: 0.1.0\n")
    write_session_store(root)


def resources(path: Path, journal: Path) -> None:
    """Publish an application-owned provider with observable startup and cleanup."""
    write(path, f'''
        from contextlib import asynccontextmanager
        from pathlib import Path
        from harnest import context, lifecycle
        from harnest.decisions import (
            Choice, ChoicePolicy, ChoiceResult, DecisionAction, DecisionBinding,
            DecisionCapabilities, DecisionDefinition, DecisionOutcome,
            DecisionResponse, Decisions, QuestionKind,
        )

        _journal = Path({str(journal)!r})

        def record(value):
            with _journal.open("a") as stream:
                stream.write(value + "\\n")

        class CustomProvider:
            capabilities = DecisionCapabilities(frozenset({{QuestionKind.CHOICE}}))
            version = "1"

            async def evaluate(self, request):
                record("evaluate")
                choice = "billing" if "charge" in request.state["message"] else "support"
                return DecisionResponse({{"team": ChoiceResult(choice)}})

        @lifecycle.resource
        @context.provider("decisions")
        @asynccontextmanager
        async def decisions():
            record("start")
            try:
                definition = DecisionDefinition("triage", "1", (
                    Choice("team", "Choose a team", {{"billing": "Charges", "support": "Help"}}),
                ))
                policy = ChoicePolicy("team", {{
                    "billing": DecisionOutcome(DecisionAction.ROUTE, "billing_agent"),
                    "support": DecisionOutcome(DecisionAction.ROUTE, "support_agent"),
                }})
                yield Decisions(providers={{"local": CustomProvider()}},
                                bindings=(DecisionBinding(definition, "local", policy),))
            finally:
                record("stop")
    ''')


class DecisionFrameworkTests(unittest.TestCase):
    def _run(self, framework: str, *, extension: bool = False, visible: bool = False) -> None:
        """Verify lazy startup, framework-neutral results and cleanup after serving."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            artifact = Path(directory) / "artifact"
            journal = Path(directory) / "journal"
            agent(root)
            resource_path = root / "lifecycle" / "decisions.py"
            if extension:
                resource_path = self._extension(root)
            resources(resource_path, journal)
            write(root / "lifecycle" / "output.py", f'''
                from harnest import lifecycle
                from harnest.output import OutputPolicy
                @lifecycle.output_policy
                def output_policy():
                    return OutputPolicy(decision_results={visible!r})
            ''')
            compile_artifact(root, artifact, framework=framework)
            self.assertFalse(journal.exists(), "compile must not initialize decision clients")
            with TestClient(create_fastapi_app(artifact)) as client:
                session = client.post("/sessions", json={})
                self.assertEqual(session.status_code, 201, session.text)
                session_id = session.json()["id"]
                self._request(client, session_id, "duplicate charge", "billing_agent", visible)
                self._request(client, session_id, "help me", "support_agent", visible)
                self._stream_request(client, session_id, visible)
                self._live_request(client, session_id, visible)
            self.assertEqual(journal.read_text().splitlines(), ["start"] + ["evaluate"] * 4 + ["stop"])

    def _extension(self, root: Path) -> Path:
        """Contribute the same resource using existing extension authority declarations."""
        path = root / "extensions" / "custom_decisions"
        write(path / "extension.yaml", '''
            apiVersion: harnest.dev/v1alpha1
            kind: Extension
            metadata:
              name: custom_decisions
              version: 0.1.0
            runtime:
              entrypoint: extension:extension
            contributes:
              lifecycle: [resources/lifecycle/]
            capabilities: [context.resources]
        ''')
        write(path / "extension.py", '''
            from harnest.extensions import Extension

            class CustomDecisions(Extension):
                """Contribute an application-owned custom decision resource."""

            extension = CustomDecisions()
        ''')
        return path / "resources" / "lifecycle" / "decisions.py"

    def _request(self, client: TestClient, session: str, message: str, expected: str, visible: bool) -> None:
        """Assert the served result came from the selected custom-provider route."""
        response = client.post("/responses", json={"input": message, "sessionId": session})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["outputText"], expected)
        self._assert_decisions(response.json()["output"], visible, expected)

    def _assert_decisions(self, output, visible, expected="billing_agent"):
        """Verify policy affects disclosure while preserving the authored route."""
        decisions = [item for item in output if item["type"] == "decision_result"]
        self.assertEqual(len(decisions), int(visible))
        if visible:
            self.assertEqual(decisions[0]["value"]["outcome"]["route"], expected)
            self.assertNotIn("duplicate charge", json.dumps(decisions))

    def _stream_request(self, client, session, visible):
        """SSE exposes the same decision once incrementally and in completed output."""
        response = client.post("/responses", json={"input": "duplicate charge", "sessionId": session, "stream": True})
        self.assertEqual(response.status_code, 200, response.text)
        frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        self._assert_frames(frames, visible)

    def _live_request(self, client, session, visible):
        """WebSocket uses the same projection and opt-in behavior as SSE and JSON."""
        with client.websocket_connect("/live") as socket:
            socket.send_json({"type": "connect", "sessionId": session})
            self.assertEqual(socket.receive_json()["type"], "session.connected")
            socket.send_json({"type": "response.create", "input": "duplicate charge"})
            frames = []
            while True:
                frame = socket.receive_json()
                frames.append(frame)
                if frame["type"] in {"response.completed", "response.failed", "error"}:
                    break
            self._assert_frames(frames, visible)

    def _assert_frames(self, frames, visible):
        """Keep hidden judgments out of all frames, including the completion envelope."""
        decisions = [frame for frame in frames if frame["type"] == "response.decision_result"]
        self.assertEqual(len(decisions), int(visible))
        completed = frames[-1]
        self.assertEqual(completed["type"], "response.completed", frames)
        self._assert_decisions(completed["output"], visible)

    def test_adk_visible_decisions(self):
        self._run("adk", visible=True)

    def test_langgraph_visible_decisions(self):
        self._run("langgraph", visible=True)

    def test_adk_decision_resource(self):
        self._run("adk")

    def test_langgraph_decision_resource(self):
        self._run("langgraph")

    def test_adk_extension_decision_resource(self):
        self._run("adk", extension=True)

    def test_langgraph_extension_decision_resource(self):
        self._run("langgraph", extension=True)
