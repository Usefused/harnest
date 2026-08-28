import unittest
import asyncio
from dataclasses import replace

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
)
from types import SimpleNamespace


def listener(phase, callback, *, order=0, name="hook"):
    return LifecycleListener(phase, callback, order, f"{name}.py", 1, name)


class ModelLifecyclePipelineTests(unittest.IsolatedAsyncioTestCase):
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


async def _response(value):
    return value


if __name__ == "__main__":
    unittest.main()
