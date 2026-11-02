import asyncio
import importlib.util
from importlib import metadata
import tempfile
import textwrap
import unittest
from pathlib import Path

from harnest.bundle import compile_artifact
from harnest.compatibility import current_harnest_version
from harnest.runtime import (
    create_fastapi_app,
    load_compiled_application,
    run_agent_message,
)
from _session_store_fixture import write_session_store


ADK_AVAILABLE = importlib.util.find_spec("google.adk") is not None
LANGGRAPH_AVAILABLE = importlib.util.find_spec("langgraph") is not None


class FrameworkArtifactTests(unittest.TestCase):
    def test_managed_artifact_executes_and_serves_across_frameworks(self):
        """Compile each backend once for direct, JSON, SSE, and live serving journeys."""

        frameworks = [
            (framework, distribution)
            for framework, distribution, available in (
                ("adk", "google-adk", ADK_AVAILABLE),
                ("langgraph", "langgraph", LANGGRAPH_AVAILABLE),
            )
            if available
        ]
        # Optional backends must be visible as a skip, not an empty passing matrix.
        if not frameworks:
            self.skipTest("ADK or LangGraph is required for compiled serving")
        for framework, distribution in frameworks:
            with self.subTest(framework=framework), tempfile.TemporaryDirectory() as tmp:
                root, output = Path(tmp) / "source", Path(tmp) / "artifact"
                self._managed_source(root)
                manifest = compile_artifact(root, output, framework=framework)
                direct = asyncio.run(run_agent_message(output, "hello"))

                self.assertEqual(manifest["harnestVersion"], current_harnest_version())
                self.assertEqual(manifest["framework"]["name"], framework)
                self.assertEqual(manifest["framework"]["mode"], "managed")
                self.assertEqual(manifest["framework"]["distribution"], distribution)
                self.assertEqual(
                    manifest["framework"]["version"], metadata.version(distribution)
                )
                self.assertEqual(load_compiled_application(output).kind, "graph")
                self.assertEqual(direct["text"], "compiled-graph")
                # LangGraph retains native state around the portable scalar result.
                result = (
                    direct["result"] if framework == "adk"
                    else direct["result"]["value"]
                )
                self.assertEqual(result, "compiled-graph")
                self._assert_managed_http_transports(output)

    def _assert_managed_http_transports(self, artifact: Path) -> None:
        """Preserve the compiled JSON/SSE/live assertions for both managed backends."""

        from fastapi.testclient import TestClient

        with TestClient(create_fastapi_app(artifact)) as client:
            session = client.post("/sessions", json={})
            self.assertEqual(session.status_code, 201)
            session_id = session.json()["id"]
            response = client.post(
                "/responses", json={"input": "hello", "sessionId": session_id}
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["outputText"], "compiled-graph")
            stream = client.post(
                "/responses",
                json={"input": "hello", "sessionId": session_id, "stream": True},
            )
            self.assertEqual(stream.status_code, 200)
            for event in (
                "response.agent_activity", "response.text.delta", "response.completed"
            ):
                self.assertIn(f"event: {event}", stream.text)
            with client.websocket_connect("/live") as websocket:
                websocket.send_json({"type": "connect", "sessionId": session_id})
                self.assertEqual(websocket.receive_json()["type"], "session.connected")
                websocket.send_json(
                    {"type": "response.create", "requestId": "one", "input": "hello"}
                )
                self.assertEqual(websocket.receive_json()["type"], "response.created")
                frames = []
                while not frames or frames[-1]["type"] != "response.completed":
                    frames.append(websocket.receive_json())
                delta = next(
                    frame for frame in frames if frame["type"] == "response.text.delta"
                )
                self.assertTrue(
                    any(frame["type"] == "response.agent_activity" for frame in frames)
                )
                self.assertEqual(delta["delta"], "compiled-graph")
                self.assertEqual(frames[-1]["status"], "completed")

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
            from harnest.agent.approval import require_human_approval
            from harnest.agent import tool


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
            root / "lifecycle" / "gateway.py",
            """
            from dataclasses import replace

            from harnest import lifecycle
            from harnest.runtime_auth import AuthPrincipal, AuthenticationError


            @lifecycle.authenticate
            def authenticate(connection, principal):
                del principal
                user_id = connection.headers.get("x-user")
                if not user_id:
                    raise AuthenticationError()
                return AuthPrincipal(user_id)


            @lifecycle.model.before
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
                root / "lifecycle" / "retrieval.py",
                f"""
                from contextlib import contextmanager
                from pathlib import Path

                from harnest import lifecycle


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
                from harnest.agent.approval import request_human_approval
                from harnest.agent import tool


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
                from harnest.agent.approval import require_human_approval
                from harnest.agent import tool


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
            (root / "lifecycle" / "checkpoints.py").write_text(
                "from harnest import lifecycle\n"
                "from harnest.lib.checkpoints import checkpoints\n"
                "@lifecycle.storage.checkpoints\n"
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
                from harnest.agent.approval import request_human_approval


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
            (root / "lifecycle" / "checkpoints.py").write_text(
                "from harnest import lifecycle\n"
                "from harnest.lib.checkpoints import checkpoints\n"
                "@lifecycle.storage.checkpoints\n"
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
