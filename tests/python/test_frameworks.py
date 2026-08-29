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
from _session_store_fixture import write_session_store


ADK_AVAILABLE = importlib.util.find_spec("google.adk") is not None
LANGGRAPH_AVAILABLE = importlib.util.find_spec("langgraph") is not None


class FrameworkArtifactTests(unittest.TestCase):
    def _write(self, path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(value).lstrip(), encoding="utf-8")
        if path.name == "agent.py" and "subagents" not in path.parts:
            write_session_store(path.parent)

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

    def _structured_source(self, root: Path) -> None:
        self._write(
            root / "agent.py",
            """
            from dataclasses import dataclass

            from harnest.graph import START, Edge, Event, Graph


            @dataclass
            class SearchHit:
                document_id: str
                score: float


            def retrieve(_value):
                return Event(
                    output={"hits": [SearchHit("guide", 0.95)]},
                    message="found one result",
                )


            root_agent = Graph(
                name="structured_graph",
                nodes={"retrieve": retrieve},
                edges=(Edge(START, "retrieve"),),
            )
            """,
        )
        self._card(root)

    def _http_route_source(self, root: Path) -> None:
        """Add a portable business endpoint backed by the compiled root agent."""

        self._write(
            root / "extensions" / "http.py",
            """
            from fastapi import APIRouter, Request
            from harnest import lifecycle


            @lifecycle.http_routes
            def http_routes(agent):
                router = APIRouter(prefix="/custom")

                @router.post("/invoke")
                async def invoke(request: Request):
                    response = await agent.invoke(
                        connection=request,
                        input="hello",
                        metadata={"source": "custom"},
                    )
                    return response.as_dict()

                return router
            """,
        )

    def _approval_source(self, root: Path) -> None:
        self._write(
            root / "agent.py",
            """
            from google.adk.models import BaseLlm, LlmResponse
            from google.genai import types
            from harnest.agent import Agent


            class ApprovalLlm(BaseLlm):
                async def generate_content_async(self, request, stream=False):
                    has_result = any(
                        part.function_response is not None
                        for content in request.contents
                        for part in content.parts
                    )
                    part = (
                        types.Part(text="approved response")
                        if has_result
                        else types.Part(function_call=types.FunctionCall(
                            id="protected-call",
                            name="protected_send",
                            args={"value": "record"},
                        ))
                    )
                    yield LlmResponse(content=types.Content(
                        role="model", parts=[part]
                    ))


            root_agent = Agent(name="approval_agent", model=ApprovalLlm(model="approval"))
            """,
        )
        self._write(root / "instructions.md", "Call protected_send once.\n")
        self._write(
            root / "tools" / "protected_send.py",
            """
            from harnest.approval import require_human_approval
            from harnest.tool import tool


            @tool
            @require_human_approval(message="Approve sending {value}?")
            def protected_send(value: str) -> str:
                \"\"\"Send one protected value.\"\"\"
                return f"sent:{value}"
            """,
        )
        self._card(root)

    def _lifecycle_source(self, root: Path) -> None:
        self._write(
            root / "agent.py",
            """
            from google.adk.models import BaseLlm, LlmResponse
            from google.genai import types
            from harnest.agent import Agent


            class LifecycleLlm(BaseLlm):
                async def generate_content_async(self, request, stream=False):
                    del stream
                    text = next(
                        part.text
                        for content in reversed(request.contents)
                        for part in reversed(content.parts)
                        if part.text
                    )
                    yield LlmResponse(content=types.Content(
                        role="model",
                        parts=[types.Part(text=f"model saw {text}")],
                    ))


            root_agent = Agent(
                name="lifecycle_agent",
                model=LifecycleLlm(model="lifecycle"),
            )
            """,
        )
        self._lifecycle_extension(root)

    def _langgraph_lifecycle_source(self, root: Path) -> None:
        self._write(
            root / "agent.py",
            """
            from langchain_core.language_models.chat_models import BaseChatModel
            from langchain_core.messages import AIMessage
            from langchain_core.outputs import ChatGeneration, ChatResult
            from harnest.agent import Agent


            class LifecycleModel(BaseChatModel):
                @property
                def _llm_type(self):
                    return "lifecycle"

                def bind_tools(self, tools, **kwargs):
                    del tools, kwargs
                    return self

                def _generate(
                    self, messages, stop=None, run_manager=None, **kwargs
                ):
                    del stop, run_manager, kwargs
                    return ChatResult(generations=[ChatGeneration(
                        message=AIMessage(
                            content=f"model saw {messages[-1].content}"
                        )
                    )])


            root_agent = Agent(
                name="lifecycle_agent",
                model=LifecycleModel(),
            )
            """,
        )
        self._lifecycle_extension(root)

    def _lifecycle_extension(self, root: Path) -> None:
        self._write(root / "instructions.md", "Answer with the model response.\n")
        self._write(
            root / "extensions" / "gateway.py",
            """
            from dataclasses import replace

            from harnest.lifecycle import lifecycle
            from harnest.runtime_auth import AuthPrincipal, AuthenticationError


            @lifecycle.authenticate
            def authenticate(connection, principal):
                del principal
                user_id = connection.headers.get("x-user")
                if not user_id:
                    raise AuthenticationError()
                return AuthPrincipal(user_id)


            @lifecycle.before_model
            def identify_model_request(context, request):
                messages = list(request.messages)
                latest = messages[-1]
                messages[-1] = replace(
                    latest,
                    text=f"checked:{context.user_id}:{latest.text}",
                )
                return replace(request, messages=tuple(messages))
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
        application = SimpleNamespace(bridge=None, output_schema=None)

        text_value, structured = _graph_output(
            application,
            {
                "messages": [message],
                "value": "stale input",
                "_harnest_turn_start": 0,
            },
        )

        self.assertEqual(text_value, "visible answer")
        self.assertEqual(structured["value"], "stale input")
        self.assertNotIn("_harnest_turn_start", structured)

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

    def test_langgraph_output_returns_configured_pydantic_result(self):
        from pydantic import BaseModel

        class Answer(BaseModel):
            value: str

        application = SimpleNamespace(bridge=None, output_schema=Answer)

        text_value, result = _graph_output(
            application,
            {"structured_response": Answer(value="structured")},
        )

        self.assertEqual(text_value, '{"value": "structured"}')
        self.assertEqual(result, {"value": "structured"})

    def test_langgraph_output_injects_declared_current_turn_metadata(self):
        from typing import Any

        from pydantic import BaseModel

        from harnest.structured import FrameworkMetadata

        class Details(BaseModel):
            langgraph: dict[str, Any]

        class Answer(BaseModel):
            value: str
            metadata: FrameworkMetadata[Details]

        application = SimpleNamespace(
            bridge=None, output_schema=Answer, kind="agent"
        )
        _, result = _graph_output(
            application,
            {
                "structured_response": {"value": "structured"},
                "messages": [{"content": "old"}, {"content": "current"}],
            },
            turn_start=1,
        )

        self.assertEqual(result["value"], "structured")
        self.assertEqual(
            result["metadata"]["langgraph"]["messages"],
            [{"content": "current"}],
        )

    def test_graph_output_schema_validates_the_terminal_value(self):
        from pydantic import BaseModel

        class Answer(BaseModel):
            value: str

        application = SimpleNamespace(
            bridge=None, output_schema=Answer, kind="graph"
        )

        _, result = _graph_output(
            application,
            {"value": {"value": "structured"}, "messages": []},
        )

        self.assertEqual(result, {"value": "structured"})

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

    def test_compiled_frameworks_preserve_structured_result_models(self):
        frameworks = (
            ("adk", ADK_AVAILABLE),
            ("langgraph", LANGGRAPH_AVAILABLE),
        )
        for framework, available in frameworks:
            if not available:
                continue
            with self.subTest(
                framework=framework
            ), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "source"
                output = Path(directory) / "artifact"
                root.mkdir()
                self._structured_source(root)

                compile_artifact(root, output, framework=framework)
                result = asyncio.run(run_agent_message(output, "retrieve"))
                structured = result["result"]
                if framework == "langgraph":
                    structured = structured["value"]

                self.assertEqual(
                    structured,
                    {"hits": [{"document_id": "guide", "score": 0.95}]},
                )

    def test_compiled_frameworks_mount_custom_routes_that_invoke_root(self):
        from fastapi.testclient import TestClient

        frameworks = (
            ("adk", ADK_AVAILABLE),
            ("langgraph", LANGGRAPH_AVAILABLE),
        )
        for framework, available in frameworks:
            if not available:
                continue
            with self.subTest(
                framework=framework
            ), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "source"
                output = Path(directory) / "artifact"
                root.mkdir()
                self._managed_source(root)
                self._http_route_source(root)

                compile_artifact(root, output, framework=framework)
                with TestClient(create_fastapi_app(output)) as client:
                    response = client.post("/custom/invoke")
                    paths = client.get("/openapi.json").json()["paths"]

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "completed")
                self.assertEqual(response.json()["outputText"], "compiled-graph")
                self.assertIn("/custom/invoke", paths)

    @unittest.skipUnless(ADK_AVAILABLE, "google-adk is not installed")
    def test_managed_adk_http_pauses_and_resumes_protected_tool(self):
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            output = Path(directory) / "artifact"
            root.mkdir()
            self._approval_source(root)
            compile_artifact(root, output, framework="adk")

            with TestClient(create_fastapi_app(output)) as client:
                session_id = client.post("/sessions", json={}).json()["id"]
                required = client.post(
                    "/responses",
                    json={"input": "send", "sessionId": session_id},
                ).json()
                resumed = client.post(
                    f"/approvals/{required['requiredAction']['id']}",
                    json={"decision": "approve"},
                )

            self.assertEqual(required["status"], "requires_action")
            self.assertEqual(resumed.status_code, 200)
            self.assertEqual(resumed.json()["status"], "completed")
            self.assertEqual(resumed.json()["outputText"], "approved response")

    @unittest.skipUnless(ADK_AVAILABLE, "google-adk is not installed")
    def test_compiled_adk_authentication_flows_into_before_model(self):
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            output = Path(directory) / "artifact"
            root.mkdir()
            self._lifecycle_source(root)
            compile_artifact(root, output, framework="adk")

            with TestClient(create_fastapi_app(output)) as client:
                rejected = client.post("/responses", json={"input": "hello"})
                session = client.post(
                    "/sessions",
                    headers={"x-user": "alice"},
                    json={},
                )
                response = client.post(
                    "/responses",
                    headers={"x-user": "alice"},
                    json={"input": "hello", "sessionId": session.json()["id"]},
                )
                session_record = client.get(
                    f"/sessions/{session.json()['id']}",
                    headers={"x-user": "alice"},
                )

            self.assertEqual(rejected.status_code, 401)
            self.assertEqual(session.status_code, 201)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["outputText"], "model saw checked:alice:hello")
            self.assertIsNone(session_record.json()["createdAt"])
            self.assertIsNotNone(session_record.json()["updatedAt"])
            self.assertTrue(session_record.json()["metadata"]["adk"]["events"])

    @unittest.skipUnless(LANGGRAPH_AVAILABLE, "langgraph is not installed")
    def test_compiled_langgraph_authentication_flows_into_before_model(self):
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            output = Path(directory) / "artifact"
            root.mkdir()
            self._langgraph_lifecycle_source(root)
            compile_artifact(root, output, framework="langgraph")

            with TestClient(create_fastapi_app(output)) as client:
                rejected = client.post("/responses", json={"input": "hello"})
                session = client.post(
                    "/sessions",
                    headers={"x-user": "alice"},
                    json={},
                )
                response = client.post(
                    "/responses",
                    headers={"x-user": "alice"},
                    json={"input": "hello", "sessionId": session.json()["id"]},
                )
                session_record = client.get(
                    f"/sessions/{session.json()['id']}",
                    headers={"x-user": "alice"},
                )

            self.assertEqual(rejected.status_code, 401)
            self.assertEqual(session.status_code, 201)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["outputText"], "model saw checked:alice:hello")
            self.assertNotIn("messages", session_record.json()["state"])
            self.assertTrue(
                session_record.json()["metadata"]["langgraph"]["messages"]
            )

    @unittest.skipUnless(LANGGRAPH_AVAILABLE, "langgraph is not installed")
    def test_compiled_runtime_owns_resource_startup_and_shutdown(self):
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            output = Path(directory) / "artifact"
            journal = Path(directory) / "resource-journal.txt"
            root.mkdir()
            self._managed_source(root)
            self._write(
                root / "extensions" / "retrieval.py",
                f"""
                from contextlib import contextmanager
                from pathlib import Path

                from harnest.lifecycle import lifecycle


                @lifecycle.resource
                @contextmanager
                def vector_client():
                    journal = Path({str(journal)!r})
                    journal.write_text("started", encoding="utf-8")
                    try:
                        yield
                    finally:
                        journal.write_text("stopped", encoding="utf-8")
                """,
            )

            compile_artifact(root, output, framework="langgraph")
            self.assertFalse(journal.exists())

            with TestClient(create_fastapi_app(output)) as client:
                # Lifespan startup now fails fast before traffic instead of
                # deferring extension acquisition to the first session call.
                self.assertEqual(journal.read_text(encoding="utf-8"), "started")
                response = client.post("/sessions", json={})
                self.assertEqual(response.status_code, 201)
                self.assertEqual(journal.read_text(encoding="utf-8"), "started")

            self.assertEqual(journal.read_text(encoding="utf-8"), "stopped")

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

    @unittest.skipUnless(ADK_AVAILABLE, "google-adk is not installed")
    def test_advanced_adk_root_uses_dynamic_approval_governance(self):
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            output = Path(directory) / "artifact"
            root.mkdir()
            self._write(
                root / "agent.py",
                """
                from google.adk.agents import BaseAgent
                from google.adk.events import Event
                from google.genai import types
                from harnest.agent import Agent
                from harnest.approval import request_human_approval
                from harnest.tool import tool


                @tool
                async def protected(value):
                    '''Run one protected native-root operation.'''
                    risk = {"capability": "network", "value": value}
                    async with request_human_approval(
                        action="advanced.send",
                        message="Approve {capability} access?",
                        arguments=risk,
                    ):
                        return f"approved:{value}"


                class ProtectedRoot(BaseAgent):
                    async def _run_async_impl(self, _context):
                        value = await protected("root")
                        yield Event(
                            author=self.name,
                            content=types.Content(
                                role="model", parts=[types.Part(text=value)]
                            ),
                        )


                root_agent = Agent.advanced(ProtectedRoot(name="advanced_root"))
                """,
            )
            self._card(root)
            compile_artifact(root, output, framework="adk", mode="advanced")

            with TestClient(
                create_fastapi_app(output), base_url="http://127.0.0.1"
            ) as client:
                client.post("/sessions", json={"id": "advanced-root"})
                response = client.post(
                    "/responses",
                    json={"input": "run", "sessionId": "advanced-root"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                required = response.json()
                self.assertEqual(required["status"], "requires_action", required)
                self.assertEqual(
                    required["requiredAction"]["action"], "dynamic:advanced.send"
                )
                completed = client.post(
                    f"/approvals/{required['requiredAction']['id']}",
                    json={"decision": "approve"},
                ).json()

            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["outputText"], "approved:root")

    @unittest.skipUnless(ADK_AVAILABLE, "google-adk is not installed")
    def test_managed_adk_root_can_embed_advanced_approved_subagent(self):
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            output = Path(directory) / "artifact"
            root.mkdir()
            self._write(
                root / "agent.py",
                """
                from google.adk.agents import BaseAgent
                from google.adk.events import Event
                from google.adk.models import BaseLlm, LlmResponse
                from google.genai import types
                from harnest.agent import Agent
                from harnest.approval import require_human_approval
                from harnest.tool import tool


                @tool
                @require_human_approval(message="Approve advanced subagent?")
                async def protected(value):
                    '''Run one protected native-subagent operation.'''
                    return f"approved:{value}"


                class ProtectedChild(BaseAgent):
                    async def _run_async_impl(self, _context):
                        value = await protected("subagent")
                        yield Event(
                            author=self.name,
                            content=types.Content(
                                role="model", parts=[types.Part(text=value)]
                            ),
                        )


                class TransferModel(BaseLlm):
                    async def generate_content_async(self, _request, stream=False):
                        del stream
                        yield LlmResponse(
                            content=types.Content(
                                role="model",
                                parts=[types.Part(function_call=types.FunctionCall(
                                    name="transfer_to_agent",
                                    args={"agent_name": "native_child"},
                                ))],
                            )
                        )


                native_child = Agent.advanced(
                    ProtectedChild(
                        name="native_child",
                        description="Handles the protected request.",
                    )
                )
                root_agent = Agent(
                    name="managed_root",
                    model=TransferModel(model="transfer"),
                    instruction="Always delegate.",
                    subagents=[native_child],
                )
                """,
            )
            self._card(root)
            self._write(root / "instructions.md", "Always delegate.")
            compile_artifact(root, output, framework="adk", mode="managed")

            with TestClient(create_fastapi_app(output)) as client:
                client.post("/sessions", json={"id": "advanced-child"})
                response = client.post(
                    "/responses",
                    json={"input": "delegate", "sessionId": "advanced-child"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                required = response.json()
                self.assertEqual(required["status"], "requires_action", required)
                completed = client.post(
                    f"/approvals/{required['requiredAction']['id']}",
                    json={"decision": "approve"},
                ).json()

            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["outputText"], "approved:subagent")

    @unittest.skipUnless(ADK_AVAILABLE, "google-adk is not installed")
    def test_embedded_advanced_adk_subagent_rejects_root_only_configuration(self):
        from google.adk.agents import LlmAgent
        from google.adk.apps import App
        from harnest.agent import Agent

        def build(child):
            return Agent(
                name="managed_root",
                model="gemini-test",
                instruction="Delegate.",
                subagents=[child],
            ).build()

        with self.assertRaisesRegex(ValueError, "name must match.*native_child"):
            build(
                Agent.advanced(
                    LlmAgent(name="native_child", model="gemini-test"),
                    name="public_child",
                )
            )
        with self.assertRaisesRegex(ValueError, "root input/output adapters"):
            build(
                Agent.advanced(
                    LlmAgent(name="native_child", model="gemini-test"),
                    input_adapter=lambda text, state: {**state, "input": text},
                )
            )
        with self.assertRaisesRegex(TypeError, "ADK App targets are root-only"):
            build(
                Agent.advanced(
                    App(
                        name="native_app",
                        root_agent=LlmAgent(
                            name="native_child", model="gemini-test"
                        ),
                    )
                )
            )

    @unittest.skipUnless(ADK_AVAILABLE, "google-adk is not installed")
    def test_managed_adk_discovers_agent_advanced_subagent_module(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            output = Path(directory) / "artifact"
            root.mkdir()
            self._write(
                root / "agent.py",
                """
                from harnest.agent import Agent


                root_agent = Agent(
                    name="managed_root",
                    model="gemini-test",
                    instruction="Delegate when appropriate.",
                )
                """,
            )
            self._write(root / "instructions.md", "Delegate when appropriate.")
            self._write(
                root / "subagents" / "native_child.py",
                """
                from google.adk.agents import LlmAgent
                from harnest.agent import Agent


                native_child = Agent.advanced(
                    LlmAgent(
                        name="native_child",
                        model="gemini-test",
                        instruction="Handle native work.",
                    )
                )
                """,
            )
            self._card(root)

            compile_artifact(root, output, framework="adk", mode="managed")
            application = load_compiled_application(output)

            self.assertEqual(application.mode, "managed")
            self.assertEqual(application.target.sub_agents[0].name, "native_child")

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
                from harnest.lib.checkpoints import checkpoints


                graph = builder.compile(
                    checkpointer=checkpoints.as_langgraph_checkpointer()
                )


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
            (root / "lib").mkdir()
            (root / "lib" / "checkpoints.py").write_text(
                "from harnest.checkpoint import MemoryStore\n"
                "checkpoints = MemoryStore()\n",
                encoding="utf-8",
            )
            (root / "extensions" / "checkpoints.py").write_text(
                "from harnest.lifecycle import lifecycle\n"
                "from harnest.lib.checkpoints import checkpoints\n"
                "@lifecycle.checkpointer\n"
                "def checkpointer(): return checkpoints\n",
                encoding="utf-8",
            )

            manifest = compile_artifact(
                root, output, framework="langgraph", mode="advanced"
            )
            result = asyncio.run(run_agent_message(output, "hello"))

            self.assertEqual(manifest["framework"]["mode"], "advanced")
            self.assertEqual(result["text"], "advanced:hello")
            self.assertEqual(result["result"], "advanced:hello")

    @unittest.skipUnless(LANGGRAPH_AVAILABLE, "langgraph is not installed")
    def test_advanced_langgraph_root_uses_dynamic_approval_governance(self):
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            output = Path(directory) / "artifact"
            root.mkdir()
            self._write(
                root / "agent.py",
                """
                from langgraph.graph import END, START, StateGraph
                from harnest.agent import Agent
                from harnest.approval import request_human_approval


                async def reply(state):
                    risk = {
                        "capabilities": ["network"],
                        "sourceHash": "sha256:fixture",
                    }
                    async with request_human_approval(
                        action="typescript.execute",
                        message="Execute TypeScript with network access?",
                        arguments=risk,
                    ):
                        return {"answer": f"approved:{state['prompt']}"}


                builder = StateGraph(dict)
                builder.add_node("reply", reply)
                builder.add_edge(START, "reply")
                builder.add_edge("reply", END)
                from harnest.lib.checkpoints import checkpoints


                graph = builder.compile(
                    checkpointer=checkpoints.as_langgraph_checkpointer()
                )


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
            (root / "lib").mkdir()
            (root / "lib" / "checkpoints.py").write_text(
                "from harnest.checkpoint import MemoryStore\n"
                "checkpoints = MemoryStore()\n",
                encoding="utf-8",
            )
            (root / "extensions" / "checkpoints.py").write_text(
                "from harnest.lifecycle import lifecycle\n"
                "from harnest.lib.checkpoints import checkpoints\n"
                "@lifecycle.checkpointer\n"
                "def checkpointer(): return checkpoints\n",
                encoding="utf-8",
            )

            compile_artifact(root, output, framework="langgraph", mode="advanced")
            with TestClient(create_fastapi_app(output)) as client:
                session_id = client.post("/sessions", json={}).json()["id"]
                required = client.post(
                    "/responses",
                    json={"input": "run", "sessionId": session_id},
                ).json()
                self.assertEqual(required["status"], "requires_action", required)
                self.assertEqual(
                    required["requiredAction"]["action"],
                    "dynamic:typescript.execute",
                )
                completed = client.post(
                    f"/approvals/{required['requiredAction']['id']}",
                    json={"decision": "approve"},
                ).json()

            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["outputText"], "approved:run")


if __name__ == "__main__":
    unittest.main()
