import unittest
import asyncio
from dataclasses import replace

from harnest.context import activate_context, context, create_agent_context
from harnest.lifecycle import (
    LifecycleListener,
    ModelCallRequest,
    ModelCallResponse,
    ModelMessage,
    LifecycleContext,
)
from harnest.model_hooks import (
    _apply_adk_request,
    _apply_adk_response,
    _apply_langgraph_request,
    _apply_langgraph_response,
    current_model_context,
    model_invocation_scope,
    ModelLifecycleError,
    ModelLifecyclePipeline,
    portable_model_extension,
    bind_model_extension,
    _adk_context,
)
from types import SimpleNamespace


def listener(phase, callback, *, order=0, name="hook"):
    return LifecycleListener(phase, callback, order, f"{name}.py", 1, name)


class ModelLifecyclePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_transitions_replace_and_finish_model_flow(self):
        seen = []

        def before(context, request):
            message = replace(request.messages[0], text="checked")
            return context.next(replace(request, messages=(message,)))

        def after_first(context, response):
            return context.finish(replace(response, text="finished"))

        def after_late(context, response):
            seen.append(response.text)
            return context.next()

        pipeline = ModelLifecyclePipeline(
            [
                listener("before_model", before),
                listener("after_model", after_first),
                listener("after_model", after_late, order=10),
            ],
            framework="adk",
        )

        async def handler(request):
            self.assertEqual(request.messages[0].text, "checked")
            return ModelCallResponse("provider")

        result = await pipeline.run(
            ModelCallRequest("model", (ModelMessage("user", "raw"),)), handler
        )

        self.assertEqual(result.text, "finished")
        self.assertEqual(seen, [])

    async def test_ordered_request_mutation_and_response_transformation(self):
        seen = []

        def first(_context, request):
            seen.append("first")
            message = replace(request.messages[0], text="checked")
            return replace(request, messages=(message,))

        async def second(_context, request):
            seen.append(request.messages[0].text)

        def after(_context, response):
            return replace(response, text=response.text.upper())

        pipeline = ModelLifecyclePipeline(
            [
                listener("before_model", second, order=20, name="second"),
                listener("before_model", first, order=10, name="first"),
                listener("after_model", after, name="after"),
            ],
            framework="adk",
        )
        request = ModelCallRequest("model", (ModelMessage("user", "raw"),))

        async def handler(value):
            self.assertEqual(value.messages[0].text, "checked")
            return ModelCallResponse("answer")

        result = await pipeline.run(request, handler)
        self.assertEqual(result.text, "ANSWER")
        self.assertEqual(seen, ["first", "checked"])

    async def test_before_listener_can_short_circuit_model_call(self):
        called = False
        pipeline = ModelLifecyclePipeline(
            [
                listener(
                    "before_model",
                    lambda _context, _request: ModelCallResponse("blocked"),
                )
            ],
            framework="langgraph",
        )

        async def handler(_request):
            nonlocal called
            called = True
            return ModelCallResponse("provider")

        result = await pipeline.run(
            ModelCallRequest(None, (ModelMessage("user", "raw"),)), handler
        )
        self.assertEqual(result.text, "blocked")
        self.assertFalse(called)

    async def test_errors_notify_all_without_masking_primary_failure(self):
        seen = []

        async def broken_observer(_context, error):
            seen.append(str(error))
            raise RuntimeError("observer")

        pipeline = ModelLifecyclePipeline(
            [listener("on_model_error", broken_observer)], framework="adk"
        )

        async def handler(_request):
            raise ValueError("provider failed")

        with self.assertRaisesRegex(ValueError, "provider failed"):
            await pipeline.run(
                ModelCallRequest(None, (ModelMessage("user", "raw"),)), handler
            )
        self.assertEqual(seen, ["provider failed"])

    async def test_invalid_replacement_is_rejected(self):
        pipeline = ModelLifecyclePipeline(
            [listener("after_model", lambda _context, _response: "wrong")],
            framework="adk",
        )
        with self.assertRaisesRegex(ModelLifecycleError, "ModelCallResponse"):
            await pipeline.run(
                ModelCallRequest(None, ()),
                lambda _request: _response(ModelCallResponse("ok")),
            )

    def test_framework_adapters_use_native_extension_types(self):
        model_listener = listener(
            "before_model", lambda _context, _request: None
        )
        from google.adk.plugins.base_plugin import BasePlugin
        from langchain.agents.middleware import AgentMiddleware

        self.assertIsInstance(
            portable_model_extension((model_listener,), framework="adk"), BasePlugin
        )
        self.assertIsInstance(
            portable_model_extension((model_listener,), framework="langgraph"),
            AgentMiddleware,
        )

    def test_adk_text_changes_preserve_tool_parts_and_response_metadata(self):
        from google.adk.models.llm_request import LlmRequest
        from google.adk.models.llm_response import LlmResponse
        from google.genai import types

        call = types.FunctionCall(name="lookup", args={"id": "1"})
        native_request = LlmRequest(
            model="old",
            contents=[
                types.Content(
                    role="user",
                    parts=[types.Part(text="raw"), types.Part(function_call=call)],
                )
            ],
        )
        original = ModelCallRequest("old", (ModelMessage("user", "raw"),))
        updated = ModelCallRequest("new", (ModelMessage("user", "checked"),))
        _apply_adk_request(native_request, original, updated)
        self.assertEqual(native_request.contents[0].parts[1].function_call.name, "lookup")

        native_response = LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part(text="answer"), types.Part(function_call=call)],
            ),
            custom_metadata={"gateway": "team"},
            interaction_id="interaction-1",
        )
        replacement = _apply_adk_response(native_response, ModelCallResponse("guarded"))
        self.assertEqual(replacement.content.parts[1].function_call.name, "lookup")
        self.assertEqual(replacement.custom_metadata, {"gateway": "team"})
        self.assertEqual(replacement.interaction_id, "interaction-1")

    def test_adk_non_text_replacement_names_the_responsible_hook_phase(self):
        from google.adk.models.llm_request import LlmRequest
        from google.adk.models.llm_response import LlmResponse
        from google.genai import types

        call = types.FunctionCall(name="lookup", args={"id": "1"})
        request = LlmRequest(
            model="model",
            contents=[
                types.Content(role="user", parts=[types.Part(function_call=call)])
            ],
        )
        original = ModelCallRequest("model", (ModelMessage("user", ""),))
        changed = ModelCallRequest("model", (ModelMessage("user", "added"),))
        with self.assertRaisesRegex(
            ModelLifecycleError,
            "before_model cannot add text to a non-text ADK message",
        ):
            _apply_adk_request(request, original, changed)

        response = LlmResponse(
            content=types.Content(
                role="model", parts=[types.Part(function_call=call)]
            )
        )
        with self.assertRaisesRegex(
            ModelLifecycleError,
            "after_model cannot add text to a non-text ADK message",
        ):
            _apply_adk_response(response, ModelCallResponse("added"))

    def test_langgraph_text_changes_preserve_tool_calls_metadata_and_structure(self):
        from langchain.agents.middleware.types import ModelResponse
        from langchain_core.messages import AIMessage, HumanMessage

        image = {"type": "image_url", "image_url": {"url": "https://images.test/1"}}
        input_message = HumanMessage(
            [{"type": "text", "text": "raw", "cache_control": "keep"}, image],
            id="input-1",
            additional_kwargs={"x": 1},
        )

        class Request:
            model = SimpleNamespace(model="openai/test")
            messages = [input_message]

            def override(self, **values):
                return SimpleNamespace(**values)

        original = ModelCallRequest(None, (ModelMessage("human", "raw"),))
        updated = ModelCallRequest(None, (ModelMessage("human", "checked"),))
        changed_request = _apply_langgraph_request(Request(), original, updated)
        self.assertEqual(changed_request.messages[0].id, "input-1")
        self.assertEqual(changed_request.messages[0].additional_kwargs, {"x": 1})
        self.assertEqual(changed_request.messages[0].content[0]["text"], "checked")
        self.assertEqual(
            changed_request.messages[0].content[0]["cache_control"], "keep"
        )
        self.assertEqual(changed_request.messages[0].content[1], image)

        message = AIMessage(
            [{"type": "text", "text": "answer", "signature": "keep"}, image],
            id="output-1",
            tool_calls=[{"name": "lookup", "args": {"id": "1"}, "id": "call-1"}],
            response_metadata={"gateway": "team"},
            usage_metadata={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        )
        native = ModelResponse(result=[message], structured_response={"ok": True})
        replacement = _apply_langgraph_response(native, ModelCallResponse("guarded"))
        output = replacement.result[0]
        self.assertEqual(output.id, "output-1")
        self.assertEqual(output.tool_calls[0]["name"], "lookup")
        self.assertEqual(output.response_metadata, {"gateway": "team"})
        self.assertEqual(output.usage_metadata["total_tokens"], 3)
        self.assertEqual(output.content[0]["text"], "guarded")
        self.assertEqual(output.content[0]["signature"], "keep")
        self.assertEqual(output.content[1], image)
        self.assertEqual(replacement.structured_response, {"ok": True})

    def test_langgraph_noop_hook_preserves_native_request_and_response_objects(self):
        from langchain.agents.middleware.types import ModelResponse
        from langchain_core.messages import AIMessage, HumanMessage

        image = {"type": "image_url", "image_url": {"url": "https://images.test/1"}}
        input_message = HumanMessage([{"type": "text", "text": "raw"}, image])
        output_message = AIMessage(
            [{"type": "text", "text": "answer"}, image],
            tool_calls=[{"name": "lookup", "args": {}, "id": "call-1"}],
            response_metadata={"gateway": "team"},
        )
        native_response = ModelResponse(
            result=[output_message], structured_response={"answer": 1}
        )

        class Request:
            model = SimpleNamespace(model="openai/test")
            messages = [input_message]

            def override(self, **values):
                raise AssertionError(f"no-op hook unexpectedly overrode {values!r}")

        middleware = portable_model_extension(
            (listener("before_model", lambda _context, _request: None),),
            framework="langgraph",
        )
        seen = []

        def handler(request):
            seen.append(request)
            return native_response

        result = middleware.wrap_model_call(Request(), handler)
        self.assertIs(seen[0].messages[0], input_message)
        self.assertIs(result, native_response)

    async def test_invocation_context_is_isolated_across_concurrent_calls(self):
        async def read(user_id):
            invocation = LifecycleContext(
                framework="adk",
                agent_name="support",
                invocation_id=f"invoke-{user_id}",
                user_id=user_id,
                session_id=f"session-{user_id}",
            )
            with model_invocation_scope(invocation):
                await asyncio.sleep(0)
                return current_model_context("adk")

        first, second = await asyncio.gather(read("one"), read("two"))
        self.assertEqual(first.user_id, "one")
        self.assertEqual(first.session_id, "session-one")
        self.assertEqual(second.user_id, "two")
        self.assertEqual(second.invocation_id, "invoke-two")
        self.assertIsNone(current_model_context("adk").user_id)

    def test_adk_callback_uses_active_subagent_without_replacing_ownership(self):
        invocation = LifecycleContext(
            framework="adk",
            agent_name="root",
            invocation_id="invoke-1",
            user_id="owner",
            session_id="session-1",
        )
        callback = SimpleNamespace(
            agent_name="researcher",
            invocation_id="native-invocation",
            user_id="untrusted-user",
            session_id="untrusted-session",
        )

        with model_invocation_scope(invocation):
            context = _adk_context(callback)

        self.assertEqual(context.agent_name, "researcher")
        self.assertEqual(context.invocation_id, "invoke-1")
        self.assertEqual(context.user_id, "owner")
        self.assertEqual(context.session_id, "session-1")

    def test_langgraph_middleware_binds_each_managed_agent_name(self):
        seen = []
        extension = portable_model_extension(
            (listener("before_model", lambda context, _request: seen.append(context)),),
            framework="langgraph",
        )
        researcher = bind_model_extension(extension, agent_name="researcher")
        writer = bind_model_extension(extension, agent_name="writer")
        request = SimpleNamespace(
            model=SimpleNamespace(model="openai/test"),
            messages=[],
            override=lambda **values: SimpleNamespace(**values),
        )
        response = SimpleNamespace(result=[])
        invocation = LifecycleContext(
            framework="langgraph",
            agent_name="root",
            invocation_id="invoke-1",
            user_id="owner",
            session_id="session-1",
        )

        with model_invocation_scope(invocation):
            researcher.wrap_model_call(request, lambda _request: response)
            writer.wrap_model_call(request, lambda _request: response)

        self.assertEqual([item.agent_name for item in seen], ["researcher", "writer"])
        self.assertEqual([item.user_id for item in seen], ["owner", "owner"])

    def test_langgraph_model_callback_scopes_general_context_to_its_agent(self):
        """Keep context resources shared while exposing the executing child."""

        seen = []

        def before(lifecycle_context, _request):
            seen.append(
                (
                    lifecycle_context.agent_name,
                    context.agent_name,
                    context.parent_agent_name,
                    context.depth,
                    context.resource("memory"),
                )
            )

        extension = portable_model_extension(
            (listener("before_model", before),), framework="langgraph"
        )
        researcher = bind_model_extension(extension, agent_name="researcher")
        request = SimpleNamespace(
            model=SimpleNamespace(model="openai/test"),
            messages=[],
            override=lambda **values: SimpleNamespace(**values),
        )
        response = SimpleNamespace(result=[])
        invocation = LifecycleContext(
            framework="langgraph",
            agent_name="root",
            invocation_id="invoke-1",
            user_id="owner",
            session_id="session-1",
        )
        active = create_agent_context(
            framework="langgraph",
            agent_name="root",
            invocation_id="invoke-1",
            user_id="owner",
            session_id="session-1",
            metadata={},
            resources={"memory": "shared"},
        )

        with activate_context(active), model_invocation_scope(invocation):
            researcher.wrap_model_call(request, lambda _request: response)

        self.assertEqual(
            seen,
            [("researcher", "researcher", "root", 1, "shared")],
        )


async def _response(value):
    return value


if __name__ == "__main__":
    unittest.main()
