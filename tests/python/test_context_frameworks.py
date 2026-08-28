import importlib.util
from pathlib import Path
import tempfile
import textwrap
import unittest

from harnest.bundle import compile_artifact
from harnest.runtime import create_fastapi_app
from _session_store_fixture import write_session_store


ADK_AVAILABLE = importlib.util.find_spec("google.adk") is not None
LANGGRAPH_AVAILABLE = importlib.util.find_spec("langgraph") is not None


class ContextFrameworkIntegrationTests(unittest.TestCase):
    def _write(self, path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(value).lstrip(), encoding="utf-8")

    def _write_agent(self, root: Path) -> None:
        self._write(
            root / "agent.py",
            """
            from harnest.graph import START, Edge, Graph
            from harnest.lib.memory.read import recall


            _nested = Graph(
                name="nested_memory",
                nodes={"recall": recall},
                edges=(Edge(START, "recall"),),
            )
            root_agent = Graph(
                name="context_graph",
                nodes={"nested": _nested},
                edges=(Edge(START, "nested"),),
            )
            """,
        )
        self._write(
            root / "lib" / "memory" / "read.py",
            """
            from harnest.context import context
            from harnest.graph import Event


            def recall(value):
                memory = context.resource("memory")
                cache = context.resource("request_cache")
                if cache is not context.resource("request_cache"):
                    raise RuntimeError("request cache was recreated inside one invocation")
                text = f"{value}:{memory['label']}:{cache['serial']}"
                return Event(output=text, message=text)
            """,
        )
        self._write(root / "instructions.md", "Use the nested memory graph.\n")
        self._write(
            root / "agent-card.yaml",
            """
            name: Context graph
            description: Exercises invocation resources in nested managed nodes.
            version: 0.1.0
            """,
        )
        write_session_store(root)

    def _write_resources(self, root: Path, journal: Path) -> None:
        self._write(
            root / "extensions" / "resources.py",
            f"""
            from contextlib import contextmanager
            from pathlib import Path

            from harnest.context import context
            from harnest.lifecycle import lifecycle


            _journal = Path({str(journal)!r})
            _request_serial = 0


            def _record(value):
                with _journal.open("a", encoding="utf-8") as stream:
                    stream.write(value + "\\n")


            @lifecycle.resource
            @context("memory")
            @contextmanager
            def memory():
                _record("memory:start")
                try:
                    yield {{"label": "shared"}}
                finally:
                    _record("memory:stop")


            @context("request_cache")
            def request_cache():
                global _request_serial
                _request_serial += 1
                _record(f"request:{{_request_serial}}")
                return {{"serial": _request_serial}}
            """,
        )

    def _write_adk_subagent(self, root: Path) -> None:
        self._write(
            root / "agent.py",
            """
            from google.adk.models import BaseLlm, LlmResponse
            from google.genai import types

            from harnest.agent import Agent
            from harnest.context import context


            class RootModel(BaseLlm):
                async def generate_content_async(self, _request, stream=False):
                    del stream
                    yield LlmResponse(content=types.Content(
                        role="model",
                        parts=[types.Part(function_call=types.FunctionCall(
                            name="transfer_to_agent",
                            args={"agent_name": "memory_child"},
                        ))],
                    ))


            class ChildModel(BaseLlm):
                async def generate_content_async(self, _request, stream=False):
                    del stream
                    memory = context.resource("memory")
                    cache = context.resource("request_cache")
                    yield LlmResponse(content=types.Content(
                        role="model",
                        parts=[types.Part(text=(
                            f"child:{memory['label']}:{cache['serial']}"
                        ))],
                    ))


            _child = Agent(
                name="memory_child",
                description="Reads the managed invocation context.",
                model=ChildModel(model="child"),
                instruction="Return the memory value.",
            )
            root_agent = Agent(
                name="context_root",
                model=RootModel(model="root"),
                instruction="Delegate every request.",
                subagents=[_child],
            )
            """,
        )
        self._write(root / "instructions.md", "Delegate every request.\n")
        self._write(
            root / "agent-card.yaml",
            """
            name: Context subagent
            description: Exercises resources inherited by an ADK child agent.
            version: 0.1.0
            """,
        )
        write_session_store(root)

    def _assert_framework_context(self, framework: str) -> None:
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / "source"
            output = workspace / "artifact"
            journal = workspace / "resource-journal.txt"
            root.mkdir()
            self._write_agent(root)
            self._write_resources(root, journal)
            compile_artifact(root, output, framework=framework)

            # Neither compile nor application construction may eagerly start a
            # user-owned client with external side effects.
            self.assertFalse(journal.exists())
            with TestClient(create_fastapi_app(output)) as client:
                session = client.post("/sessions", json={})
                self.assertEqual(session.status_code, 201, session.text)
                session_id = session.json()["id"]
                first = client.post(
                    "/responses",
                    json={"input": "first", "sessionId": session_id},
                )
                second = client.post(
                    "/responses",
                    json={"input": "second", "sessionId": session_id},
                )

                self.assertEqual(first.status_code, 200, first.text)
                self.assertEqual(second.status_code, 200, second.text)
                self.assertEqual(first.json()["outputText"], "first:shared:1")
                self.assertEqual(second.json()["outputText"], "second:shared:2")
                self.assertEqual(
                    journal.read_text(encoding="utf-8").splitlines(),
                    ["memory:start", "request:1", "request:2"],
                )

            self.assertEqual(
                journal.read_text(encoding="utf-8").splitlines(),
                ["memory:start", "request:1", "request:2", "memory:stop"],
            )

    @unittest.skipUnless(ADK_AVAILABLE, "google-adk is not installed")
    def test_adk_nested_managed_node_inherits_compiled_context(self):
        self._assert_framework_context("adk")

    @unittest.skipUnless(ADK_AVAILABLE, "google-adk is not installed")
    def test_adk_managed_subagent_inherits_compiled_context(self):
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / "source"
            output = workspace / "artifact"
            journal = workspace / "resource-journal.txt"
            root.mkdir()
            self._write_adk_subagent(root)
            self._write_resources(root, journal)
            compile_artifact(root, output, framework="adk")

            with TestClient(create_fastapi_app(output)) as client:
                session = client.post("/sessions", json={}).json()
                response = client.post(
                    "/responses",
                    json={"input": "delegate", "sessionId": session["id"]},
                )

                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["outputText"], "child:shared:1")

    @unittest.skipUnless(LANGGRAPH_AVAILABLE, "langgraph is not installed")
    def test_langgraph_nested_managed_node_inherits_compiled_context(self):
        self._assert_framework_context("langgraph")


if __name__ == "__main__":
    unittest.main()
