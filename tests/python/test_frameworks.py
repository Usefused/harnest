import asyncio
import importlib.util
from importlib import metadata
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace

from harnest.bundle import compile_artifact
from harnest.compatibility import current_harnest_version
from harnest.runtime import (
    create_fastapi_app,
    load_compiled_application,
    run_agent_message,
)
from harnest.runtime_langgraph import _graph_output, _tool_events


ADK_AVAILABLE = importlib.util.find_spec("google.adk") is not None
LANGGRAPH_AVAILABLE = importlib.util.find_spec("langgraph") is not None


class FrameworkArtifactTests(unittest.TestCase):
    def _write(self, path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(value).lstrip(), encoding="utf-8")

    def _card(self, root: Path) -> None:
        self._write(
            root / "agent-card.yaml",
            """
            name: Deterministic graph
            description: Exercises the framework-neutral graph contract.
            version: 0.1.0
            """,
        )

    def _managed_source(self, root: Path) -> None:
        self._write(
            root / "agent.py",
            """
            from harnest.graph import START, Edge, Event, Graph


            def prepare(_value):
                return "compiled"


            def finish(value):
                return Event(output=f"{value}-graph", message=f"{value}-graph")


            root_agent = Graph(
                name="portable_graph",
                nodes={"prepare": prepare, "finish": finish},
                edges=(Edge(START, "prepare"), Edge("prepare", "finish")),
            )
            """,
        )
        self._card(root)

    def test_langgraph_output_extracts_structured_cloud_model_text(self):
        message = SimpleNamespace(
            content=[
                {"type": "thinking", "thinking": "internal"},
                {"type": "text", "text": "visible answer"},
            ]
        )
        application = SimpleNamespace(bridge=None)

        text_value, structured = _graph_output(
            application, {"messages": [message], "value": "stale input"}
        )

        self.assertEqual(text_value, "visible answer")
        self.assertEqual(structured["value"], "stale input")

        thought_only = SimpleNamespace(
            content=[{"type": "thinking", "thinking": "private reasoning"}]
        )
        self.assertEqual(
            _graph_output(application, {"messages": [thought_only]}),
            ("", None),
        )

        call = SimpleNamespace(
            type="ai",
            tool_calls=[{"id": "call-1", "name": "echo", "args": {"text": "ok"}}],
        )
        tool_result = SimpleNamespace(
            type="tool",
            tool_call_id="call-1",
            name="echo",
            content="ok",
        )
        self.assertEqual(
            _tool_events({"messages": [call, tool_result]}),
            [
                {
                    "type": "tool_call",
                    "id": "call-1",
                    "name": "echo",
                    "arguments": {"text": "ok"},
                },
                {
                    "type": "tool_result",
                    "id": "call-1",
                    "name": "echo",
                    "result": "ok",
                },
            ],
        )

    @unittest.skipUnless(ADK_AVAILABLE, "google-adk is not installed")
    def test_managed_adk_artifact_executes_without_provisioner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            output = Path(directory) / "artifact"
            root.mkdir()
            self._managed_source(root)

            manifest = compile_artifact(root, output, framework="adk")
            result = asyncio.run(run_agent_message(output, "hello"))

            self.assertEqual(manifest["harnestVersion"], current_harnest_version())
            self.assertEqual(manifest["framework"]["name"], "adk")
            self.assertEqual(manifest["framework"]["mode"], "managed")
            self.assertEqual(manifest["framework"]["distribution"], "google-adk")
            self.assertEqual(
                manifest["framework"]["version"], metadata.version("google-adk")
            )
            self.assertEqual(result["text"], "compiled-graph")
            self.assertEqual(result["result"], "compiled-graph")
            self.assertEqual(load_compiled_application(output).kind, "graph")

    @unittest.skipUnless(LANGGRAPH_AVAILABLE, "langgraph is not installed")
    def test_managed_langgraph_http_json_sse_and_live(self):
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            output = Path(directory) / "artifact"
            root.mkdir()
            self._managed_source(root)

            manifest = compile_artifact(root, output, framework="langgraph")
            direct = asyncio.run(run_agent_message(output, "hello"))
            app = create_fastapi_app(output)
            with TestClient(app) as client:
                session = client.post("/sessions", json={})
                self.assertEqual(session.status_code, 201)
                session_id = session.json()["id"]
                response = client.post(
                    "/responses",
                    json={"input": "hello", "sessionId": session_id},
                )
                stream = client.post(
                    "/responses",
                    json={"input": "hello", "sessionId": session_id, "stream": True},
                )
                with client.websocket_connect("/live") as websocket:
                    websocket.send_json({"type": "connect", "sessionId": session_id})
                    connected = websocket.receive_json()
                    websocket.send_json(
                        {"type": "response.create", "requestId": "one", "input": "hello"}
                    )
                    created = websocket.receive_json()
                    delta = websocket.receive_json()
                    completed = websocket.receive_json()

            self.assertEqual(manifest["harnestVersion"], current_harnest_version())
            self.assertEqual(manifest["framework"]["name"], "langgraph")
            self.assertEqual(manifest["framework"]["mode"], "managed")
            self.assertEqual(manifest["framework"]["distribution"], "langgraph")
            self.assertEqual(
                manifest["framework"]["version"], metadata.version("langgraph")
            )
            self.assertEqual(direct["text"], "compiled-graph")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["outputText"], "compiled-graph")
            self.assertIn("event: response.text.delta", stream.text)
            self.assertIn("event: response.completed", stream.text)
            self.assertEqual(connected["type"], "session.connected")
            self.assertEqual(created["type"], "response.created")
            self.assertEqual(delta["delta"], "compiled-graph")
            self.assertEqual(completed["status"], "completed")

    @unittest.skipUnless(ADK_AVAILABLE, "google-adk is not installed")
    def test_advanced_adk_mode_accepts_a_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            output = Path(directory) / "artifact"
            root.mkdir()
            self._write(
                root / "agent.py",
                """
                from google.adk.workflow import START, Edge, Workflow, node
                from harnest.agent import Agent


                async def produce(_value):
                    yield "native-adk"


                step = node(produce, name="produce")
                root_agent = Agent.advanced(
                    Workflow(
                        name="native_adk",
                        edges=[Edge(from_node=START, to_node=step)],
                    )
                )
                """,
            )
            self._card(root)

            manifest = compile_artifact(
                root, output, framework="adk", mode="advanced"
            )
            application = load_compiled_application(output)

            self.assertEqual(manifest["framework"]["mode"], "advanced")
            self.assertEqual(application.kind, "advanced")
            self.assertEqual(application.target.name, "native_adk")

    @unittest.skipUnless(LANGGRAPH_AVAILABLE, "langgraph is not installed")
    def test_advanced_langgraph_mode_uses_explicit_bridge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            output = Path(directory) / "artifact"
            root.mkdir()
            self._write(
                root / "agent.py",
                """
                from langgraph.graph import END, START, StateGraph
                from harnest.agent import Agent


                def reply(state):
                    return {"answer": f"advanced:{state['prompt']}"}


                builder = StateGraph(dict)
                builder.add_node("reply", reply)
                builder.add_edge(START, "reply")
                builder.add_edge("reply", END)
                graph = builder.compile()


                def input_adapter(text, state):
                    return {**state, "prompt": text}


                def output_adapter(state):
                    return state["answer"]


                root_agent = Agent.advanced(
                    graph,
                    name="native_langgraph",
                    input_adapter=input_adapter,
                    output_adapter=output_adapter,
                )
                """,
            )
            self._card(root)

            manifest = compile_artifact(
                root, output, framework="langgraph", mode="advanced"
            )
            result = asyncio.run(run_agent_message(output, "hello"))

            self.assertEqual(manifest["framework"]["mode"], "advanced")
            self.assertEqual(result["text"], "advanced:hello")
            self.assertEqual(result["result"], "advanced:hello")


if __name__ == "__main__":
    unittest.main()
