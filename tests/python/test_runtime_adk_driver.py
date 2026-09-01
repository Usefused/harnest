import base64
import json
import types as python_types
import unittest
from dataclasses import replace
from typing import Any, ClassVar
from unittest.mock import patch

from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.apps.app import ResumabilityConfig
from google.adk.models import BaseLlm, LlmResponse
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel

from harnest._adk_warnings import suppress_adk_warnings
from harnest.agent import Agent
from harnest.application import CompiledApplication
from harnest.assets import AssetMediaMetadata, AssetScope, MemoryAssetStore
from harnest.checkpoint import MemoryStore, PendingAction, RunScope
from harnest.content import Image
from harnest.durable import (
    NativeDurableSuspended,
    NativeResumeInput,
    adk_durable_tool,
    current_native_durable_call,
)
from harnest.external_continuation import PendingExternalContinuation
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
from harnest.runtime_adk import (
    ADKRuntimeDriver,
    _ADKEventNormalizer,
    _asset_content_plugin,
    _function_response_items,
    _register_agent_context_plugins,
    _register_mcp_context_plugins,
    _register_tool_lifecycle_plugin,
)
from harnest.structured import FrameworkMetadata, provider_output_schema
from harnest.transient_media import (
    TransientMediaAccess,
    TransientMediaLeaseStore,
    TransientMediaScope,
)
from harnest.tool import tool


_MODEL_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
    b"\x00\x00\x00\x00"
    b"\x00\x00\x00\x00IEND\x00\x00\x00\x00"
)


class DeterministicLlm(BaseLlm):
    async def generate_content_async(self, llm_request, stream=False):
        del llm_request, stream
        yield LlmResponse(
            content=types.Content(
                role="model", parts=[types.Part(text="deterministic response")]
            )
        )


class UnknownArgumentLlm(BaseLlm):
    """Issue one invalid call, then record Harnest's repair response."""

    responses: ClassVar[list[dict[str, Any]]] = []

    async def generate_content_async(self, llm_request, stream=False):
        del stream
        function_responses = [
            part.function_response
            for content in llm_request.contents
            for part in content.parts
            if part.function_response is not None
        ]
        if function_responses:
            self.responses.append(dict(function_responses[-1].response))
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part(text="invalid call rejected")],
                )
            )
            return
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            id="call-invalid",
                            name="search_threads",
                            args={
                                "limit": 1,
                                "startedBefore": "private-value",
                            },
                        )
                    )
                ],
            )
        )


class RecordingTurnLlm(BaseLlm):
    """Record root requests to verify turn-only history at the ADK boundary."""

    seen: ClassVar[list[list[str]]] = []

    async def generate_content_async(self, llm_request, stream=False):
        del stream
        self.seen.append(
            [
                part.text
                for content in llm_request.contents
                for part in content.parts
                if part.text
            ]
        )
        yield LlmResponse(
            content=types.Content(
                role="model", parts=[types.Part(text="turn response")]
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


class ADKManagedPluginOrderingTests(unittest.TestCase):
    def test_context_and_tool_governance_bracket_authored_plugins(self):
        manager = python_types.SimpleNamespace(
            plugins=[python_types.SimpleNamespace(name="authored")]
        )

        _register_agent_context_plugins(manager)
        _register_mcp_context_plugins(
            manager,
            python_types.SimpleNamespace(tools=[], sub_agents=[], graph=None),
            (),
        )
        _register_tool_lifecycle_plugin(manager, ())

        self.assertEqual(
            [item.name for item in manager.plugins],
            [
                "_harnest_agent_context_enter",
                "_harnest_mcp_context_enter",
                "_harnest_portable_tool_lifecycle",
                "authored",
                "_harnest_mcp_context_exit",
                "_harnest_agent_context_exit",
            ],
        )


class StructuredLlm(BaseLlm):
    async def generate_content_async(self, llm_request, stream=False):
        del llm_request, stream
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part(text='{"answer":"validated"}')],
            )
        )


class MultimodalLlm(BaseLlm):
    """Capture the transient model request for reference-boundary assertions."""

    requests: ClassVar[list[Any]] = []

    async def generate_content_async(self, llm_request, stream=False):
        del stream
        self.requests.append(llm_request.model_copy(deep=True))
        yield LlmResponse(
            content=types.Content(
                role="model", parts=[types.Part(text="saw image")]
            )
        )


class MediaOutputLlm(BaseLlm):
    """Return one inline provider image for output-staging assertions."""

    async def generate_content_async(self, llm_request, stream=False):
        del llm_request, stream
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        inline_data=types.Blob(
                            mime_type="image/png", data=_MODEL_PNG
                        )
                    )
                ],
            )
        )


class DurableResumeLlm(BaseLlm):
    """Issue one stable durable call, then report its injected response."""

    responses: ClassVar[list[dict[str, Any]]] = []

    async def generate_content_async(self, llm_request, stream=False):
        del stream
        function_responses = [
            part.function_response
            for content in llm_request.contents
            for part in content.parts
            if part.function_response is not None
        ]
        if function_responses:
            response = function_responses[-1]
            self.responses.append(
                {
                    "id": response.id,
                    "name": response.name,
                    "response": dict(response.response),
                }
            )
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=f"resumed:{response.response['report']}")],
                )
            )
            return
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            id="call-1",
                            name="wait_for_job",
                            args={"job_id": "job-1"},
                        )
                    )
                ],
            )
        )


class _RecordingCheckpointStore(MemoryStore):
    """Record begin calls so a resume cannot silently start a second run."""

    def __init__(self) -> None:
        super().__init__()
        self.begin_calls = 0

    async def begin_run(self, **kwargs):
        """Count run creation attempts before delegating to atomic storage."""

        self.begin_calls += 1
        return await super().begin_run(**kwargs)


def _durable_resume_application(
    store: _RecordingCheckpointStore,
    observations: dict[str, list[Any]],
) -> CompiledApplication:
    """Build a fresh ADK application replica over shared durable state."""

    @tool(durable=True)
    async def wait_for_job(job_id: str) -> dict[str, Any]:
        """Suspend until an external job supplies its durable result."""

        active = current_native_durable_call()
        assert active is not None
        observations["executions"].append(job_id)
        observations["artifacts"].append(active.artifact)
        scope = RunScope(
            "durable_resume", "test-user", "durable-session", "portable-run"
        )
        checkpoint = await store.get_checkpoint(scope=scope, namespace="events")
        # The model event must be committed before ADK advances into this tool;
        # otherwise another replica would not have the unresolved call identity.
        observations["checkpoint_payloads"].append(
            None if checkpoint is None else json.loads(checkpoint.payload)
        )
        await store.transition(
            scope=scope,
            expected_status="running",
            status="waiting",
            pending_action=PendingAction(
                "external_continuation", "continuation-1", "durable.test"
            ),
        )
        return active.suspend(
            PendingExternalContinuation("continuation-1", "durable.test")
        )

    native_tool = adk_durable_tool(wait_for_job)
    agent = LlmAgent(
        name="durable_resume",
        description="Durable resume test agent",
        model=DurableResumeLlm(model="durable-resume"),
        instruction="Call wait_for_job once, then report its result.",
        tools=[native_tool],
    )
    with suppress_adk_warnings("resumability"):
        app = App(
            name="durable_resume",
            root_agent=agent,
            resumability_config=ResumabilityConfig(is_resumable=True),
        )
    return CompiledApplication(
        name="durable_resume",
        framework="adk",
        mode="managed",
        target=agent,
        native_app=app,
        checkpointer=store,
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


def _strict_tool_application(executions: list[int]) -> CompiledApplication:
    """Build a managed ADK app whose model invents one undeclared filter."""

    @tool
    async def search_threads(limit: int = 50) -> dict[str, int]:
        """Return a bounded newest-first page."""

        executions.append(limit)
        return {"limit": limit}

    agent = LlmAgent(
        name="strict_tools",
        model=UnknownArgumentLlm(model="unknown-argument"),
        instruction="Exercise strict tool inputs.",
        tools=[search_threads],
    )
    return CompiledApplication(
        name="strict_tools",
        framework="adk",
        mode="managed",
        target=agent,
        native_app=App(name="strict_tools", root_agent=agent),
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


def _multimodal_application() -> CompiledApplication:
    """Build an ADK fixture that records the final provider request."""

    agent = LlmAgent(
        name="multimodal",
        model=MultimodalLlm(model="multimodal"),
        instruction="Inspect the supplied media.",
    )
    return CompiledApplication(
        name="multimodal",
        framework="adk",
        mode="managed",
        target=agent,
        native_app=App(name="multimodal", root_agent=agent),
    )


def _media_output_application() -> CompiledApplication:
    """Build an ADK fixture that produces provider-owned inline media."""

    agent = LlmAgent(
        name="media_output",
        model=MediaOutputLlm(model="media-output"),
        instruction="Return a test image.",
    )
    return CompiledApplication(
        name="media_output",
        framework="adk",
        mode="managed",
        target=agent,
        native_app=App(name="media_output", root_agent=agent),
    )


async def _asset_chunks(value: bytes):
    """Adapt deterministic test bytes to the streaming store contract."""

    yield value


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
        await self.driver.create_session(
            session_id="session-2", user_id="test-user", state={}
        )
        listed = await self.driver.list_sessions(user_id="test-user")
        self.assertEqual(
            [session.id for session in listed], ["session-1", "session-2"]
        )
        page = await self.driver.list_sessions(
            user_id="test-user", after="session-1", limit=1
        )
        self.assertEqual([session.id for session in page], ["session-2"])

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

    async def test_managed_adk_rejects_unknown_arguments_in_a_complete_turn(self):
        executions: list[int] = []
        UnknownArgumentLlm.responses = []
        driver = ADKRuntimeDriver(_strict_tool_application(executions))
        try:
            await driver.create_session(
                session_id="strict-arguments",
                user_id="test-user",
                state={},
            )
            result = await driver.invoke(_request("strict-arguments"))
        finally:
            await driver.close()

        self.assertEqual(result.text, "invalid call rejected")
        self.assertEqual(executions, [])
        error = UnknownArgumentLlm.responses[0]["error"]
        self.assertIn("unknown input parameters: startedBefore", error)
        self.assertNotIn("private-value", error)

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

    async def test_turn_history_root_runs_without_prior_session_contents(self):
        """Keep ADK's workflow-only single_turn mode away from app roots."""

        RecordingTurnLlm.seen.clear()
        agent = Agent(
            name="turn_root",
            model=RecordingTurnLlm(model="recording-turn"),
            instruction="Answer only the current turn.",
            history="turn",
        ).build()
        application = CompiledApplication(
            name="turn_root",
            framework="adk",
            mode="managed",
            target=agent,
            native_app=App(name="turn_root", root_agent=agent),
        )
        driver = ADKRuntimeDriver(application)
        try:
            await driver.create_session(
                session_id="turn-session", user_id="test-user", state={}
            )
            await driver.invoke(_request("turn-session", invocation_id="turn-1"))
            await driver.invoke(_request("turn-session", invocation_id="turn-2"))
        finally:
            await driver.close()

        self.assertEqual(agent.mode, "chat")
        self.assertEqual(agent.include_contents, "none")
        self.assertEqual(
            RecordingTurnLlm.seen,
            [["private prompt"], ["private prompt"]],
        )

    async def test_durable_tool_resumes_on_a_fresh_driver_replica(self):
        """Resume ADK by persisted identity without replaying the tool frame."""

        store = _RecordingCheckpointStore()
        service = InMemorySessionService()
        observations: dict[str, list[Any]] = {
            "executions": [],
            "artifacts": [],
            "checkpoint_payloads": [],
        }
        DurableResumeLlm.responses.clear()
        await store.start()
        self.addAsyncCleanup(store.close)
        first_driver = ADKRuntimeDriver(
            _durable_resume_application(store, observations),
            session_service=service,
        )
        try:
            await first_driver.create_session(
                session_id="durable-session", user_id="test-user", state={}
            )
            with self.assertRaises(NativeDurableSuspended):
                await first_driver.invoke(
                    InvocationRequest(
                        input="start",
                        user_id="test-user",
                        session_id="durable-session",
                        invocation_id="portable-run",
                        metadata={"phase": "initial"},
                        state_delta={"count": 1},
                    )
                )
        finally:
            await first_driver.close()

        scope = RunScope(
            "durable_resume", "test-user", "durable-session", "portable-run"
        )
        waiting = await store.get_run(scope=scope)
        self.assertIsNotNone(waiting)
        assert waiting is not None
        self.assertEqual(waiting.status, "waiting")
        self.assertEqual(store.begin_calls, 1)
        self.assertEqual(observations["executions"], ["job-1"])
        checkpoint_payload = observations["checkpoint_payloads"][0]
        self.assertIsNotNone(checkpoint_payload)
        checkpoint_json = json.dumps(checkpoint_payload)
        self.assertIn("call-1", checkpoint_json)
        self.assertIn("longRunningToolIds", checkpoint_json)
        artifact = observations["artifacts"][0]

        # Completion claims the waiting run before it dispatches the resume to
        # whichever replica currently owns the session execution lease.
        await store.transition(
            scope=scope,
            expected_status="waiting",
            status="running",
        )
        second_driver = ADKRuntimeDriver(
            _durable_resume_application(store, observations),
            session_service=service,
        )
        try:
            resumed = await second_driver.invoke(
                InvocationRequest(
                    input=NativeResumeInput(artifact, {"report": "ready"}),
                    user_id="test-user",
                    session_id="durable-session",
                    invocation_id="portable-run",
                    metadata={"phase": "resume"},
                    state_delta={"count": 99},
                )
            )
            session = await service.get_session(
                app_name="durable_resume",
                user_id="test-user",
                session_id="durable-session",
            )
        finally:
            await second_driver.close()

        self.assertEqual(resumed.text, "resumed:ready")
        self.assertEqual(observations["executions"], ["job-1"])
        self.assertEqual(store.begin_calls, 1)
        self.assertEqual(
            DurableResumeLlm.responses,
            [
                {
                    "id": artifact.tool_call_id,
                    "name": artifact.tool_name,
                    "response": {"report": "ready"},
                }
            ],
        )
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(session.state["count"], 1)
        self.assertEqual(
            {event.invocation_id for event in session.events},
            {artifact.native_invocation_id},
        )
        completed = await store.get_run(scope=scope)
        self.assertIsNotNone(completed)
        assert completed is not None
        self.assertEqual(completed.status, "completed")

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

    async def test_multimodal_input_materializes_only_at_model_boundary(self):
        store = MemoryAssetStore(max_asset_bytes=128, max_total_bytes=512)
        scope = AssetScope(user_id="test-user", session_id="media-session")
        record = await store.save(
            scope=scope,
            media_type="image/png",
            chunks=_asset_chunks(b"private-image"),
        )
        MultimodalLlm.requests.clear()
        driver = ADKRuntimeDriver(_multimodal_application(), asset_store=store)
        try:
            await driver.create_session(
                session_id=scope.session_id, user_id=scope.user_id, state={}
            )
            request = InvocationRequest(
                input={
                    "parts": [
                        {"type": "text", "text": "inspect"},
                        {"type": "image", "assetId": record.asset_id},
                        {"type": "data", "value": {"ticket": "T-1"}},
                    ]
                },
                user_id=scope.user_id,
                session_id=scope.session_id,
                invocation_id="media-invocation",
                metadata={},
                state_delta={},
            )
            result = await driver.invoke(request)
            messages = await driver.get_session_messages(
                session_id=scope.session_id, user_id=scope.user_id
            )
            native = await driver._runner.session_service.get_session(
                app_name="multimodal",
                user_id=scope.user_id,
                session_id=scope.session_id,
            )
        finally:
            await driver.close()

        self.assertEqual(result.text, "saw image")
        provider_parts = MultimodalLlm.requests[-1].contents[-1].parts
        self.assertEqual(provider_parts[0].text, "inspect")
        self.assertEqual(provider_parts[1].inline_data.data, b"private-image")
        self.assertEqual(provider_parts[1].inline_data.mime_type, "image/png")
        self.assertEqual(provider_parts[2].text, '{"ticket": "T-1"}')
        self.assertIsNotNone(messages)
        user_message = next(message for message in messages if message.role == "user")
        self.assertEqual(
            user_message.content,
            [
                {"type": "text", "text": "inspect"},
                {"type": "image", "assetId": record.asset_id},
                {"type": "data", "value": {"ticket": "T-1"}},
            ],
        )
        native_payload = native.model_dump(mode="json", by_alias=True)
        self.assertNotIn("private-image", str(native_payload))
        self.assertNotIn("data", native_payload["events"][0]["content"]["parts"][1])

    async def test_inline_structured_input_is_leased_before_adk_persistence(self):
        class InlineRequest(BaseModel):
            prompt: str
            screenshot: Image

        application = replace(
            _multimodal_application(), input_schema=InlineRequest
        )
        driver = ADKRuntimeDriver(application)
        scope = TransientMediaScope("test-user", "inline-input", "inline-call")
        leases = TransientMediaLeaseStore(max_total_bytes=512)
        access = TransientMediaAccess(leases, scope)
        encoded = base64.b64encode(_MODEL_PNG).decode("ascii")
        MultimodalLlm.requests.clear()
        try:
            await driver.create_session(
                session_id=scope.session_id, user_id=scope.user_id, state={}
            )
            request = InvocationRequest(
                input={
                    "prompt": "inspect",
                    "screenshot": {
                        "type": "image",
                        "data": encoded,
                        "mediaType": "image/png",
                    },
                },
                user_id=scope.user_id,
                session_id=scope.session_id,
                invocation_id=scope.call_id,
                metadata={},
                state_delta={},
            )
            with patch(
                "harnest.runtime_adk.current_transient_media",
                return_value=access,
            ):
                await driver.invoke(request)
            native = await driver._runner.session_service.get_session(
                app_name="multimodal",
                user_id=scope.user_id,
                session_id=scope.session_id,
            )
        finally:
            await driver.close()

        parts = MultimodalLlm.requests[-1].contents[-1].parts
        self.assertEqual(parts[-1].inline_data.data, _MODEL_PNG)
        self.assertEqual(parts[-1].inline_data.mime_type, "image/png")
        self.assertNotIn(encoded, repr(native))
        self.assertEqual(leases.total_bytes, 0)

    async def test_multimodal_reference_requires_asset_store(self):
        await self.driver.create_session(
            session_id="missing-store", user_id="test-user", state={}
        )
        request = InvocationRequest(
            input={"type": "image", "assetId": "asset_missing_reference"},
            user_id="test-user",
            session_id="missing-store",
            invocation_id="missing-store-invocation",
            metadata={},
            state_delta={},
        )
        with self.assertRaisesRegex(RuntimeError, "requires an asset store"):
            await self.driver.invoke(request)

    async def test_subagent_client_tool_media_is_model_only_and_retry_safe(self):
        scope = TransientMediaScope("test-user", "media-session", "call-1")
        leases = TransientMediaLeaseStore(max_total_bytes=128)
        lease = leases.stage(
            scope=scope,
            kind="image",
            media_type="image/png",
            data=b"private-image",
            metadata=AssetMediaMetadata(width=1, height=1),
        )
        access = TransientMediaAccess(store=leases, scope=scope)
        access.bind((lease.lease_id,))
        marker = {
            "type": "image",
            "mediaType": "image/png",
            "content": "attached",
        }
        original = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id="tool-1",
                        name="capture",
                        response={"screenshot": marker},
                    )
                )
            ],
        )
        callback = python_types.SimpleNamespace(
            user_id=scope.user_id,
            session=python_types.SimpleNamespace(id=scope.session_id),
            invocation_id=scope.call_id,
            agent_name="vision_child",
            branch="root.vision_child",
            node_path="root/vision_child",
        )
        plugin = _asset_content_plugin(None)
        first = python_types.SimpleNamespace(contents=[original])

        with patch(
            "harnest.runtime_adk.current_transient_media", return_value=access
        ):
            await plugin.before_model_callback(
                callback_context=callback, llm_request=first
            )
            await plugin.on_model_error_callback(
                callback_context=callback,
                llm_request=first,
                error=RuntimeError("provider failed"),
            )
            retry = python_types.SimpleNamespace(contents=[original])
            await plugin.before_model_callback(
                callback_context=callback, llm_request=retry
            )

        self.assertNotIn("harnestTransient", repr(original))
        self.assertNotIn(lease.lease_id, repr(original))
        self.assertNotIn("harnestTransient", repr(first.contents))
        self.assertEqual(
            first.contents[0].parts[-1].inline_data.data, b"private-image"
        )
        self.assertEqual(
            retry.contents[0].parts[-1].inline_data.data, b"private-image"
        )
        self.assertIsNotNone(access.peek(lease.lease_id))

        await plugin.after_model_callback(
            callback_context=callback,
            llm_response=python_types.SimpleNamespace(),
        )
        self.assertIsNone(access.peek(lease.lease_id))
        stale = python_types.SimpleNamespace(contents=[original])
        with patch(
            "harnest.runtime_adk.current_transient_media", return_value=access
        ):
            await plugin.before_model_callback(
                callback_context=callback, llm_request=stale
            )
        self.assertNotIn("harnestTransient", repr(stale.contents))
        self.assertIsNone(stale.contents[0].parts[-1].inline_data)
        event = python_types.SimpleNamespace(
            get_function_responses=lambda: [
                types.FunctionResponse(
                    id="tool-1",
                    name="capture",
                    response={"screenshot": marker},
                )
            ]
        )
        self.assertNotIn("harnestTransient", repr(_function_response_items(event)))

    async def test_model_media_is_staged_before_session_persistence(self):
        store = MemoryAssetStore(max_asset_bytes=128, max_total_bytes=512)
        driver = ADKRuntimeDriver(_media_output_application(), asset_store=store)
        try:
            await driver.create_session(
                session_id="media-output", user_id="test-user", state={}
            )
            await driver.invoke(_request("media-output"))
            messages = await driver.get_session_messages(
                session_id="media-output", user_id="test-user"
            )
            native = await driver._runner.session_service.get_session(
                app_name="media_output",
                user_id="test-user",
                session_id="media-output",
            )
        finally:
            await driver.close()

        self.assertIsNotNone(messages)
        assistant = next(message for message in messages if message.role == "assistant")
        self.assertEqual(assistant.content[0]["type"], "image")
        asset_id = assistant.content[0]["assetId"]
        self.assertIsNotNone(
            await store.stat(
                scope=AssetScope("test-user", "media-output"),
                asset_id=asset_id,
            )
        )
        native_payload = native.model_dump(mode="json", by_alias=True)
        self.assertNotIn("model-image", str(native_payload))
        self.assertIsNone(
            native_payload["events"][-1]["content"]["parts"][0]["inlineData"]
        )

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
