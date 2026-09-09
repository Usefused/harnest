"""Exercise real ADK/LangChain boundaries using deterministic local models."""

from typing import Any
import unittest

from google.adk.apps import App
from google.adk.tools import FunctionTool
from google.adk.models import BaseLlm, LlmResponse
from google.adk.models.llm_request import LlmRequest
from google.genai import types
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from harnest.agent import Agent
from harnest.application import CompiledApplication
from harnest.backends.langgraph import lower_agent
from harnest.runtime_adk import ADKRuntimeDriver
from harnest.runtime_extensions import ExtensionRuntimeDriver
from harnest.runtime_langgraph import LangGraphRuntimeDriver
from harnest.runtime_contract import InvocationRequest
from harnest.token_adk import wrap_adk_model
from harnest.token_langgraph import token_middleware
from harnest.tokens import TokenBudgetExceeded, TokenCount, TokenPolicy, token_policy_scope


class RecordingADK(BaseLlm):
    """Record exactly what the provider receives without network access."""

    requests: list[Any] = Field(default_factory=list)

    async def generate_content_async(self, llm_request, stream=False):
        """Produce one final response with real-shaped usage metadata."""
        self.requests.append(llm_request)
        if stream:
            yield LlmResponse(partial=True, content=types.Content(role="model", parts=[types.Part(text="ok")]))
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text="ok")]),
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=12, candidates_token_count=2, total_token_count=14,
            ),
        )


class RecordingLangGraph(FakeMessagesListChatModel):
    """Keep model-visible context and implement the normal tool binding seam."""

    requests: list[Any] = Field(default_factory=list)

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """Permit deterministic managed agent construction with native tools."""
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """Capture model messages separately from persisted graph state."""
        self.requests.append((messages, kwargs))
        return super()._generate(messages, stop, run_manager, **kwargs)


def lookup() -> str:
    """Find a record in the deterministic fixture."""
    return "record"


class LoopADK(RecordingADK):
    """Request a tool on each pass to exercise model-call admission."""

    async def generate_content_async(self, llm_request, stream=False):
        """Return an actual native function call for the managed tool loop."""
        self.requests.append(llm_request)
        yield LlmResponse(content=types.Content(role="model", parts=[
            types.Part(function_call=types.FunctionCall(name="lookup", args={})),
        ]))


def adk_request():
    """Include system text, tool schema, call correlation and old/new turns."""
    return LlmRequest(
        model="test", contents=[
            types.Content(role="user", parts=[types.Part(text="old")]),
            types.Content(role="model", parts=[types.Part(text="old answer")]),
            types.Content(role="user", parts=[types.Part(text="new")]),
        ], config=types.GenerateContentConfig(
            system_instruction="keep this instruction", max_output_tokens=7,
            tools=[types.Tool(function_declarations=[types.FunctionDeclaration(name="lookup", description="Find a record")])],
        ), tools_dict={"lookup": FunctionTool(lookup)},
    )


def graph_request():
    """Use a real middleware request with full native message metadata."""
    return ModelRequest(
        model=RecordingLangGraph(responses=[AIMessage(content="ok")]),
        messages=[HumanMessage(content="old"), AIMessage(content="old answer"), HumanMessage(content="new")],
        system_message=SystemMessage(content="keep this instruction"), tools=[],
    )


class TokenAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_adk_observation_and_disabled_policy_preserve_identity(self):
        for policy in (None, TokenPolicy(), TokenPolicy(enabled=False)):
            with self.subTest(policy=policy):
                delegate = RecordingADK(model="test")
                wrapped = wrap_adk_model(delegate, policy, "a")
                original = adk_request()
                _ = [item async for item in wrapped.generate_content_async(original)]
                self.assertIs(delegate.requests[0], original)
        self.assertIs(wrap_adk_model(delegate, None, "a"), delegate)

    async def test_adk_reduction_preserves_history_and_native_stricter_limit(self):
        reports = []
        policy = TokenPolicy(enforce=True, keep_recent_turns=1, max_output_tokens=20, observer=reports.append)
        delegate = RecordingADK(model="test")
        wrapped = wrap_adk_model(delegate, policy, "a")
        original = adk_request()
        _ = [item async for item in wrapped.generate_content_async(original, stream=True)]
        reduced = delegate.requests[0]
        self.assertEqual(len(reduced.contents), 1)
        self.assertEqual(len(original.contents), 3)
        self.assertEqual(reduced.config.max_output_tokens, 7)
        self.assertEqual(reduced.config.system_instruction, original.config.system_instruction)
        self.assertEqual([item.phase for item in reports], ["before", "after"])
        self.assertEqual(reports[-1].total_tokens, 14)

    async def test_adk_custom_tools_and_override_disable(self):
        delegate = RecordingADK(model="test")
        policy = TokenPolicy(tool_selector=lambda _r: [])
        wrapped = wrap_adk_model(delegate, policy, "a")
        original = adk_request()
        _ = [item async for item in wrapped.generate_content_async(original)]
        self.assertEqual(delegate.requests[-1].tools_dict, {})
        self.assertEqual(len(original.config.tools[0].function_declarations), 1)
        with token_policy_scope(None):
            _ = [item async for item in wrapped.generate_content_async(original)]
        self.assertIs(delegate.requests[-1], original)

    async def test_langgraph_observation_preserves_request_identity(self):
        middleware = token_middleware(TokenPolicy(), "a")[0]
        original = graph_request()
        async def handler(native):
            """Require observation to leave the native request untouched."""
            self.assertIs(native, original)
            return ModelResponse(result=[AIMessage(content="ok")])
        await middleware.awrap_model_call(original, handler)
        self.assertEqual(token_middleware(None, "a"), ())

    async def test_langgraph_sync_and_async_reduction_match(self):
        original = graph_request()
        reports = []
        middleware = token_middleware(TokenPolicy(keep_recent_turns=1, enforce=True, max_output_tokens=15, observer=reports.append), "a")[0]
        seen = []
        def handler(native):
            """Capture the transformed request and provider-shaped usage."""
            seen.append(native)
            return ModelResponse(result=[AIMessage(content="ok", usage_metadata={"input_tokens": 4, "output_tokens": 2, "total_tokens": 6})])
        async def async_handler(native):
            """Use the same native completion through the async boundary."""
            return handler(native)
        middleware.wrap_model_call(original, handler)
        await middleware.awrap_model_call(original, async_handler)
        self.assertEqual(seen[0].messages, seen[1].messages)
        self.assertEqual(len(seen[0].messages), 1)
        self.assertEqual(len(original.messages), 3)
        self.assertEqual(seen[0].model_settings["max_tokens"], 15)
        self.assertEqual(reports[-1].total_tokens, 6)

    async def test_both_backends_block_before_provider_dispatch(self):
        policy = TokenPolicy(enforce=True, max_input_tokens=1, count_tokens=lambda _r: TokenCount(10, False))
        delegate = RecordingADK(model="test")
        wrapped = wrap_adk_model(delegate, policy, "a")
        with self.assertRaises(TokenBudgetExceeded):
            _ = [item async for item in wrapped.generate_content_async(adk_request())]
        self.assertEqual(delegate.requests, [])
        middleware = token_middleware(policy, "a")[0]
        with self.assertRaises(TokenBudgetExceeded):
            await middleware.awrap_model_call(graph_request(), None)


class TokenRuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_runtimes_preserve_transcripts_across_stream_and_invoke(self):
        for framework in ("adk", "langgraph"):
            with self.subTest(framework=framework):
                await self._exercise_runtime(framework)

    async def test_runtime_limits_stop_tool_loops_before_second_model_call(self):
        """Share call accounting across model/tool cycles in both frameworks."""
        for framework in ("adk", "langgraph"):
            with self.subTest(framework=framework):
                reports = []
                policy = TokenPolicy(enforce=True, max_model_calls=1, observer=reports.append)
                driver, model = _driver(framework, policy, loop=True)
                try:
                    await driver.create_session(session_id="s", user_id="u", state={})
                    invocation = InvocationRequest(input="lookup", session_id="s", user_id="u", invocation_id="loop", metadata={}, state_delta={})
                    with self.assertRaises(TokenBudgetExceeded):
                        _ = [event async for event in driver.stream(invocation)]
                    self.assertEqual(len(model.requests), 1)
                    self.assertEqual(reports[-1].phase, "blocked")
                    self.assertEqual(reports[-1].exceeded, ("model_calls",))
                finally:
                    await driver.close()

    async def _exercise_runtime(self, framework):
        """Prove retention affects provider input without deleting session history."""
        policy = TokenPolicy(keep_recent_turns=1, max_model_calls=1, enforce=True)
        driver, model = _driver(framework, policy)
        try:
            await driver.create_session(session_id="s", user_id="u", state={})
            await driver.invoke(InvocationRequest(input="old", session_id="s", user_id="u", invocation_id="first", metadata={}, state_delta={}))
            events = [event async for event in driver.stream(InvocationRequest(input="new", session_id="s", user_id="u", invocation_id="second", metadata={}, state_delta={}))]
            self.assertTrue(events)
            last = model.requests[-1]
            visible = str(last.contents) if framework == "adk" else str(last[0])
            self.assertNotIn("old", visible)
            transcript = await driver.get_session_messages(session_id="s", user_id="u")
            self.assertIn("old", str(transcript))
            self.assertIn("new", str(transcript))
        finally:
            await driver.close()


def _driver(framework, policy, *, loop=False):
    """Build the actual compiled-application/runtime boundary for each backend."""
    if framework == "adk":
        model = LoopADK(model="test") if loop else RecordingADK(model="test")
        target = Agent(name="root", model=model, instruction="answer", token_policy=policy, tools=[lookup] if loop else []).build()
        app = CompiledApplication(name="root", framework="adk", mode="managed", target=target, native_app=App(name="root", root_agent=target))
        native = ADKRuntimeDriver(app)
    else:
        reply = AIMessage(content="", tool_calls=[{"name": "lookup", "args": {}, "id": "a"}]) if loop else AIMessage(content="ok")
        model = RecordingLangGraph(responses=[reply])
        definition = Agent(name="root", model=model, instruction="answer", token_policy=policy, tools=[lookup] if loop else [])
        app = CompiledApplication(name="root", framework="langgraph", mode="managed", target=lower_agent(definition, checkpointer=InMemorySaver()))
        native = LangGraphRuntimeDriver(app)
    return ExtensionRuntimeDriver(native, []), model
