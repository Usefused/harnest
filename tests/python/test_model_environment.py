"""Real framework adapters against an intercepted OpenAI-compatible HTTP API."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from harnest.bundle import EvalSuite
from harnest.evaluation import eval_config
from harnest.eval_model_transport import eval_model_transports
from harnest.model import LiteLLMModel
from harnest.model_transport import model_transport_bindings
from test_eval_transport_http import _exercise_models


class _CompletionRecorder:
    """Return OpenAI-compatible completions and retain sanitized wire metadata only."""

    def __init__(self):
        """Prepare one response for each model lane without real credentials."""

        self.requests = []
        self._replies = iter(("Paris.", '{"is_the_agent_response_valid": "valid"}', "Confirm Paris."))

    def respond(self, request):
        """Observe endpoint selection at the final OpenAI-compatible HTTP boundary."""

        payload = json.loads(request.content)
        self.requests.append({
            "host": request.url.host, "path": request.url.path,
            "model": payload["model"], "bearer_auth": "authorization" in request.headers,
        })
        return httpx.Response(200, request=request, json={
            "id": "test-completion", "object": "chat.completion", "created": 1,
            "model": payload["model"],
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": next(self._replies),
                "reasoning_content": ('{"is_the_agent_response_valid": "invalid"}'
                                      if len(self.requests) == 2 else ""),
            }}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })


def _authored_eval_config(path):
    """Omit judge and simulator models so the shared environment supplies both."""

    path.write_text(json.dumps({
        "criteria": {"final_response_match_v2": {
            "threshold": 1.0, "judgeModelOptions": {"numSamples": 1},
        }},
        "userSimulatorConfig": {},
    }), encoding="utf-8")
    return eval_config(EvalSuite((), path), "business")


class ModelEnvironmentAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_judge_and_simulator_share_compatible_http_contract(self):
        """Exercise real ADK and LangGraph callers without OpenAI credential fallback."""

        for framework in ("adk", "langgraph"):
            with self.subTest(framework=framework):
                await self._assert_compatible_routing(framework)

    async def _assert_compatible_routing(self, framework):
        """Keep each framework's native HTTP requests isolated from process settings."""

        recorder = _CompletionRecorder()

        async def send(_client, request, **_kwargs):
            """Intercept final HTTP after LiteLLM has selected the provider."""

            self.assertEqual(request.headers["authorization"], "Bearer synthetic-compatible-key")
            return recorder.respond(request)

        environment = {
            "OPENAI_MODEL": "team/chosen", "OPENAI_BASE_URL": "http://models-contract.invalid/v1",
            "OPENAI_API_KEY": "synthetic-compatible-key",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        }
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, environment, clear=True
        ), patch("httpx.AsyncClient.send", new=send), patch(
            "httpx.Client.send", side_effect=AssertionError("synchronous HTTP is forbidden")
        ), patch("litellm.telemetry", False):
            owner = LiteLLMModel.from_openai_environment().build_for(framework)
            config = _authored_eval_config(Path(directory) / "test_config.json")
            # Runtime transport must retain the captured authority, not read
            # a later command's process-wide provider settings.
            os.environ.update(OPENAI_BASE_URL="https://unrelated.invalid/v1", OPENAI_API_KEY="unrelated-key")
            score, follow_up, models = await _exercise_models(owner, framework, config)

        self.assertEqual(score, 1.0)
        self.assertEqual(follow_up, "Confirm Paris.")
        self.assertEqual(models, ("openai/team/chosen",) * 2)
        self.assertEqual(recorder.requests, [{
            "host": "models-contract.invalid", "path": "/v1/chat/completions",
            "model": "team/chosen", "bearer_auth": True,
        }] * 3)

    async def test_implicit_evaluator_endpoint_does_not_require_an_agent_binding(self):
        """Carry the configured endpoint even for agents with unrelated providers."""

        from google.adk.models.registry import LLMRegistry
        from google.adk.models.llm_request import LlmRequest
        from google.genai import types

        recorder = _CompletionRecorder()

        async def send(_client, request, **_kwargs):
            """Verify default transport configuration reaches native HTTP."""

            self.assertEqual(request.headers["authorization"], "Bearer not-required")
            return recorder.respond(request)

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "OPENAI_MODEL": "team/chosen", "OPENAI_BASE_URL": "http://custom-models.invalid/v1",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        }, clear=True), patch("httpx.AsyncClient.send", new=send), patch(
            "httpx.Client.send", side_effect=AssertionError("synchronous HTTP is forbidden")
        ), patch("litellm.telemetry", False):
            config = _authored_eval_config(Path(directory) / "test_config.json")
            with eval_model_transports(SimpleNamespace(), config) as prepared:
                adapter = LLMRegistry.new_llm(prepared.user_simulator_config.model)
                request = LlmRequest(model=adapter.model, contents=[
                    types.Content(role="user", parts=[types.Part(text="Which city?")]),
                ])
                responses = [response async for response in adapter.generate_content_async(request)]

        self.assertEqual(responses[0].content.parts[0].text, "Paris.")
        self.assertEqual(recorder.requests, [{
            "host": "custom-models.invalid", "path": "/v1/chat/completions",
            "model": "team/chosen", "bearer_auth": True,
        }])


class ModelDefaultsTests(unittest.TestCase):
    """Require explicit, vendor-neutral OpenAI-compatible configuration."""

    def test_missing_configuration_never_selects_a_provider(self):
        for environment, missing in (
            ({}, "OPENAI_MODEL"),
            ({"OPENAI_MODEL": "chosen"}, "OPENAI_BASE_URL"),
            ({"OLLAMA_MODEL": "legacy", "OLLAMA_BASE_URL": "http://legacy"}, "OPENAI_MODEL"),
        ):
            with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(ValueError, missing):
                    LiteLLMModel.from_openai_environment()

    def test_environment_is_captured_and_explicit_options_win(self):
        environment = {"OPENAI_MODEL": " team/chosen ", "OPENAI_BASE_URL": " https://models.test/v1 ", "OPENAI_API_KEY": "synthetic-key"}
        with patch.dict(os.environ, environment, clear=True):
            connector = LiteLLMModel.from_openai_environment(temperature=0)
            override = LiteLLMModel.from_openai_environment(api_base="http://override.test/v1", api_key="explicit-key")
        self.assertEqual(connector.model, "openai/team/chosen")
        self.assertEqual(connector.completion_args, {"api_base": "https://models.test/v1", "api_key": "synthetic-key", "temperature": 0})
        self.assertNotIn("synthetic-key", repr(connector))
        self.assertEqual(override.completion_args["api_base"], "http://override.test/v1")
        self.assertEqual(override.completion_args["api_key"], "explicit-key")

    def test_invalid_settings_fail_before_build(self):
        for model, endpoint in ((" ", "http://models.test/v1"), ("chosen", " "), ("chosen", "/v1"), ("chosen", "ftp://models.test")):
            with self.subTest(model=model, endpoint=endpoint), patch.dict(os.environ, {"OPENAI_MODEL": model, "OPENAI_BASE_URL": endpoint}, clear=True):
                with self.assertRaises(ValueError):
                    LiteLLMModel.from_openai_environment()

    def test_both_frameworks_retain_transport_for_evaluation(self):
        with patch.dict(os.environ, {"OPENAI_MODEL": "chosen", "OPENAI_BASE_URL": "http://models.test/v1"}, clear=True):
            connector = LiteLLMModel.from_openai_environment()
        for adapter in (connector.build(), connector.build_langgraph()):
            binding, = model_transport_bindings(adapter)
            self.assertEqual(binding.model, "openai/chosen")
            self.assertEqual(binding._completion_args["api_base"], "http://models.test/v1")
            self.assertEqual(binding._completion_args["api_key"], "not-required")

    def test_native_connector_is_removed(self):
        import harnest.model

        self.assertFalse(hasattr(harnest.model, "OllamaModel"))
