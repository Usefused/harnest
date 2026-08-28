import types as python_types
import unittest
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.models import BaseLlm, LlmResponse
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel

from harnest.application import CompiledApplication
from harnest.graph import START, Edge, Event, Graph
from harnest.mcp import MCPClient
from harnest.mcp_lifecycle import (
    MCPClientContext,
    MCPClientLifecycle,
    attach_mcp_lifecycle,
)
from harnest.neutral_runtime import (
    InvocationRequest,
    RuntimeDriver,
    SessionConflictError,
)
from harnest.output import OutputPolicy
from harnest.runtime_adk import ADKRuntimeDriver, _ADKEventNormalizer
from harnest.structured import FrameworkMetadata, provider_output_schema


class DeterministicLlm(BaseLlm):
    async def generate_content_async(self, llm_request, stream=False):
        del llm_request, stream
        yield LlmResponse(
            content=types.Content(
                role="model", parts=[types.Part(text="deterministic response")]
            )
        )


class StructuredAnswer(BaseModel):
    answer: str


class ADKTurnDetails(BaseModel):
    events: list[dict[str, Any]]


class RuntimeDetails(BaseModel):
    adk: ADKTurnDetails


class StructuredAnswerWithMetadata(BaseModel):
    answer: str
    metadata: FrameworkMetadata[RuntimeDetails]


class StructuredLlm(BaseLlm):
    async def generate_content_async(self, llm_request, stream=False):
        del llm_request, stream
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part(text='{"answer":"validated"}')],
            )
        )


class _RecordingMCPLifecycle(MCPClientLifecycle):
    """Record ADK application-level lifecycle ownership."""

    def __init__(self):
        self.events = []

    async def start(self, context: MCPClientContext):
        self.events.append(f"start:{context.framework}")

    async def close(self, context: MCPClientContext):
        self.events.append(f"close:{context.framework}")


def _application() -> CompiledApplication:
    agent = LlmAgent(
        name="root",
        description="Deterministic test agent",
        model=DeterministicLlm(model="deterministic"),
        instruction="Answer deterministically.",
    )
    app = App(name="root", root_agent=agent)
    return CompiledApplication(
        name="root",
        framework="adk",
        mode="managed",
        target=agent,
        native_app=app,
    )


def _structured_application() -> CompiledApplication:
    """Build an ADK fixture with Harnest-owned structured output metadata."""

    agent = LlmAgent(
        name="structured",
        model=StructuredLlm(model="structured"),
        instruction="Answer with the configured schema.",
        output_schema=StructuredAnswer,
    )
    return CompiledApplication(
        name="structured",
        framework="adk",
        mode="managed",
        target=agent,
        native_app=App(name="structured", root_agent=agent),
        output_schema=StructuredAnswer,
    )


def _structured_metadata_application() -> CompiledApplication:
    """Build a fixture whose public model opts into native turn metadata."""

    provider_schema = provider_output_schema(StructuredAnswerWithMetadata)
    agent = LlmAgent(
        name="structured_metadata",
        model=StructuredLlm(model="structured-metadata"),
        instruction="Answer with the provider-owned output fields.",
        output_schema=provider_schema,
    )
    return CompiledApplication(
        name="structured_metadata",
        framework="adk",
        mode="managed",
        target=agent,
        native_app=App(name="structured_metadata", root_agent=agent),
        output_schema=StructuredAnswerWithMetadata,
    )


def _structured_graph_application() -> CompiledApplication:
    """Build a portable graph whose terminal value has an output contract."""

    def answer(_value):
        return Event(output={"answer": "validated"})

    graph = Graph(
        name="structured_graph",
        nodes={"answer": answer},
        edges=(Edge(START, "answer"),),
        output_schema=StructuredAnswer,
    )
    target = graph.build()
    return CompiledApplication(
        name=graph.name,
        framework="adk",
        mode="managed",
        target=target,
        native_app=App(name=graph.name, root_agent=target),
        kind="graph",
        output_schema=graph.output_schema,
    )


def _request(session_id: str, *, invocation_id: str = "invocation-1"):
    return InvocationRequest(
        input="private prompt",
        user_id="test-user",
        session_id=session_id,
        invocation_id=invocation_id,
        metadata={"request_kind": "test"},
        state_delta={},
    )


class ADKRuntimeDriverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.driver = ADKRuntimeDriver(
            _application(),
            card={"name": "Test card", "description": "Card description"},
            extra_endpoints={"native": "/run"},
        )

    async def asyncTearDown(self):
        await self.driver.close()

    async def test_implements_contract_and_owns_session_crud(self):
        self.assertIsInstance(self.driver, RuntimeDriver)
        self.assertEqual(self.driver.info.name, "Test card")
        self.assertEqual(self.driver.info.framework, "adk")
        self.assertEqual(self.driver.info.extra_endpoints, {"native": "/run"})

        created = await self.driver.create_session(
            session_id="session-1",
            user_id="test-user",
            state={"count": 1},
        )
        self.assertEqual(created.id, "session-1")
        self.assertEqual(created.user_id, "test-user")
        self.assertEqual(created.state, {"count": 1})

        with self.assertRaises(SessionConflictError):
            await self.driver.create_session(
                session_id="session-1",
                user_id="test-user",
                state={},
            )

        updated = await self.driver.update_session(
            session_id="session-1",
            user_id="test-user",
            state_delta={"count": 2, "ready": True},
        )
        self.assertIsNotNone(updated)
        self.assertEqual(updated.state, {"count": 2, "ready": True})
        listed = await self.driver.list_sessions(user_id="test-user")
        self.assertEqual([session.id for session in listed], ["session-1"])

        self.assertTrue(
            await self.driver.delete_session(
                session_id="session-1", user_id="test-user"
            )
        )
        self.assertFalse(
            await self.driver.delete_session(
                session_id="session-1", user_id="test-user"
            )
        )
        self.assertIsNone(
            await self.driver.get_session(
                session_id="session-1", user_id="test-user"
            )
        )

    async def test_injected_adk_session_service_is_used_by_runner(self):
        service = InMemorySessionService()
        driver = ADKRuntimeDriver(_application(), session_service=service)
        try:
            await driver.create_session(
                session_id="injected", user_id="test-user", state={"ready": True}
            )
            stored = await service.get_session(
                app_name="root", user_id="test-user", session_id="injected"
            )
            self.assertEqual(stored.state, {"ready": True})
        finally:
            await driver.close()

    async def test_invoke_and_stream_use_the_same_normalized_events(self):
        await self.driver.create_session(
            session_id="invoke-session", user_id="test-user", state={}
        )
        result = await self.driver.invoke(_request("invoke-session"))
        self.assertEqual(result.text, "deterministic response")
        self.assertEqual(result.session_id, "invoke-session")
        self.assertEqual(result.metadata, {"request_kind": "test"})
        self.assertEqual(
            [event["type"] for event in result.events], ["message"]
        )
        session = await self.driver.get_session(
            session_id="invoke-session", user_id="test-user"
        )
        self.assertIsNotNone(session)
        self.assertEqual(session.metadata["adk"]["appName"], "root")
        self.assertGreaterEqual(len(session.metadata["adk"]["events"]), 1)
        self.assertIn("content", session.metadata["adk"]["events"][0])
        messages = await self.driver.get_session_messages(
            session_id="invoke-session", user_id="test-user"
        )
        self.assertIsNotNone(messages)
        self.assertIn("user", {message.role for message in messages})
        self.assertIn("assistant", {message.role for message in messages})
        assistant = next(
            message for message in messages if message.role == "assistant"
        )
        self.assertEqual(assistant.content, "deterministic response")
        self.assertIn("adk", assistant.metadata)
        self.assertIsNone(
            await self.driver.get_session_messages(
                session_id="missing", user_id="test-user"
            )
        )
        await self.driver.create_session(
            session_id="stream-session", user_id="test-user", state={}
        )
        events = [
            event
            async for event in self.driver.stream(
                _request("stream-session", invocation_id="invocation-2")
            )
        ]
        self.assertEqual(
            events,
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "text": "deterministic response",
                }
            ],
        )

    async def test_structured_output_is_a_result_for_invoke_and_stream(self):
        driver = ADKRuntimeDriver(_structured_application())
        try:
            await driver.create_session(
                session_id="structured-invoke", user_id="test-user", state={}
            )
            invoked = await driver.invoke(_request("structured-invoke"))
            await driver.create_session(
                session_id="structured-stream", user_id="test-user", state={}
            )
            streamed = [
                event
                async for event in driver.stream(
                    _request("structured-stream", invocation_id="structured-2")
                )
            ]
        finally:
            await driver.close()

        expected = {"answer": "validated"}
        self.assertEqual(invoked.result, expected)
        self.assertEqual(invoked.events[-1]["output"], expected)
        self.assertEqual(streamed[-1]["output"], expected)

    async def test_declared_runtime_metadata_contains_native_adk_turn(self):
        driver = ADKRuntimeDriver(_structured_metadata_application())
        try:
            await driver.create_session(
                session_id="metadata", user_id="test-user", state={}
            )
            invoked = await driver.invoke(_request("metadata"))
        finally:
            await driver.close()

        self.assertEqual(invoked.result["answer"], "validated")
        events = invoked.result["metadata"]["adk"]["events"]
        self.assertTrue(events)
        self.assertTrue(
            any(event.get("author") == "structured_metadata" for event in events)
        )

    async def test_graph_output_schema_validates_terminal_output(self):
        driver = ADKRuntimeDriver(_structured_graph_application())
        try:
            await driver.create_session(
                session_id="graph-output", user_id="test-user", state={}
            )
            invoked = await driver.invoke(_request("graph-output"))
        finally:
            await driver.close()

        self.assertEqual(invoked.result, {"answer": "validated"})

    async def test_mcp_gateway_lifecycle_follows_adk_driver_ownership(self):
        lifecycle = _RecordingMCPLifecycle()
        client = MCPClient.sse(
            "https://gateway.example/sse", lifecycle=lifecycle
        )
        binding = client._lifecycle_binding("adk")
        self.assertIsNotNone(binding)
        application = _application()
        assert binding is not None
        attach_mcp_lifecycle(application.target, binding)
        driver = ADKRuntimeDriver(application)
        try:
            await driver.create_session(
                session_id="lifecycle-session",
                user_id="test-user",
                state={},
            )
            await driver.invoke(_request("lifecycle-session"))
            self.assertEqual(lifecycle.events, ["start:adk"])
        finally:
            await driver.close()

        self.assertEqual(lifecycle.events, ["start:adk", "close:adk"])

    async def test_close_is_idempotent_and_rejects_new_work(self):
        await self.driver.close()
        await self.driver.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await self.driver.list_sessions(user_id="test-user")

    async def test_normalizer_filters_thoughts_and_preserves_tool_trace(self):
        event = python_types.SimpleNamespace(
            partial=False,
            content=python_types.SimpleNamespace(
                parts=[
                    python_types.SimpleNamespace(text="hidden", thought=True),
                    python_types.SimpleNamespace(text="visible", thought=False),
                ]
            ),
            output={"route": "done"},
            node_info=None,
            get_function_calls=lambda: [
                python_types.SimpleNamespace(
                    id="call-1", name="lookup", args={"query": "safe"}
                )
            ],
            get_function_responses=lambda: [
                python_types.SimpleNamespace(
                    id="call-1", name="lookup", response={"value": 3}
                )
            ],
        )
        self.assertEqual(
            _ADKEventNormalizer().feed(event),
            [
                {"type": "message", "role": "assistant", "text": "visible"},
                {
                    "type": "graph_output",
                    "output": {"route": "done"},
                    "result": {"route": "done"},
                },
                {
                    "type": "tool_call",
                    "id": "call-1",
                    "name": "lookup",
                    "arguments": {"query": "safe"},
                },
                {
                    "type": "tool_result",
                    "id": "call-1",
                    "name": "lookup",
                    "result": {"value": 3},
                },
            ],
        )
        thought_only = python_types.SimpleNamespace(
            partial=False,
            content=python_types.SimpleNamespace(
                parts=[python_types.SimpleNamespace(text="private", thought=True)]
            ),
            output=None,
            get_function_calls=lambda: [],
            get_function_responses=lambda: [],
        )
        self.assertEqual(_ADKEventNormalizer().feed(thought_only), [])

    async def test_subagent_pre_tool_narration_is_configurable(self):
        partial = python_types.SimpleNamespace(
            author="researcher",
            partial=True,
            content=python_types.SimpleNamespace(
                parts=[python_types.SimpleNamespace(text="I'll ", thought=False)]
            ),
            output=None,
            get_function_calls=lambda: [],
            get_function_responses=lambda: [],
        )
        completed = python_types.SimpleNamespace(
            author="researcher",
            partial=False,
            content=python_types.SimpleNamespace(
                parts=[
                    python_types.SimpleNamespace(
                        text="I'll inspect the page", thought=False
                    )
                ]
            ),
            output=None,
            get_function_calls=lambda: [
                python_types.SimpleNamespace(
                    id="call-1", name="inspect", args={"selector": "main"}
                )
            ],
            get_function_responses=lambda: [],
        )
        canonical = python_types.SimpleNamespace(
            author="root",
            partial=False,
            content=python_types.SimpleNamespace(
                parts=[python_types.SimpleNamespace(text="Done", thought=False)]
            ),
            output=None,
            get_function_calls=lambda: [],
            get_function_responses=lambda: [],
        )

        suppressed = _ADKEventNormalizer(root_agent_name="root")
        suppressed_events = [
            *suppressed.feed(partial),
            *suppressed.feed(completed),
            *suppressed.feed(canonical),
        ]
        included = _ADKEventNormalizer(
            OutputPolicy(subagent_messages="include"), root_agent_name="root"
        )
        included_events = [
            *included.feed(partial),
            *included.feed(completed),
            *included.feed(canonical),
        ]

        self.assertEqual(
            [event["type"] for event in suppressed_events], ["tool_call", "message"]
        )
        self.assertEqual(suppressed_events[-1]["text"], "Done")
        self.assertEqual(
            "".join(
                event["text"]
                for event in included_events
                if event["type"] == "message"
            ),
            "I'll inspect the pageDone",
        )
        self.assertIn("tool_call", [event["type"] for event in included_events])

        terminal_child = _ADKEventNormalizer(root_agent_name="root")
        terminal = python_types.SimpleNamespace(
            author="researcher",
            partial=False,
            content=python_types.SimpleNamespace(
                parts=[python_types.SimpleNamespace(text="Final answer", thought=False)]
            ),
            output=None,
            get_function_calls=lambda: [],
            get_function_responses=lambda: [],
        )
        self.assertEqual(terminal_child.feed(terminal), [])
        self.assertEqual(
            terminal_child.finish(),
            [{"type": "message", "role": "assistant", "text": "Final answer"}],
        )


if __name__ == "__main__":
    unittest.main()
