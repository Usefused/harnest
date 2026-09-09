"""Offline native-provider coverage for Harnest's canonical model environment."""

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
from harnest.model import OllamaModel
from test_eval_transport_http import _exercise_models


class _CompletionRecorder:
    """Return native Ollama completions and retain sanitized wire metadata only."""

    def __init__(self):
        """Prepare one response for each model lane without real credentials."""

        self.requests = []
        self._replies = iter(("Paris.", '{"is_the_agent_response_valid": "valid"}', "Confirm Paris."))

    def respond(self, request):
        """Observe endpoint selection at the final native Ollama HTTP boundary."""

        payload = json.loads(request.content)
        self.requests.append({
            "host": request.url.host, "path": request.url.path,
            "model": payload["model"], "bearer_auth": "authorization" in request.headers,
        })
        return httpx.Response(200, request=request, json={
            "model": payload["model"], "done": True, "done_reason": "stop",
            "message": {"role": "assistant", "content": next(self._replies),
                        "thinking": ('{"is_the_agent_response_valid": "invalid"}'
                                     if len(self.requests) == 2 else "")},
            "prompt_eval_count": 1, "eval_count": 1,
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
    async def test_agent_judge_and_simulator_share_native_ollama_http_contract(self):
        """Exercise real ADK and LangGraph callers without OpenAI credential fallback."""

        for framework in ("adk", "langgraph"):
            with self.subTest(framework=framework):
                await self._assert_native_ollama_routing(framework)

    async def _assert_native_ollama_routing(self, framework):
        """Keep each framework's native HTTP requests isolated from process settings."""

        recorder = _CompletionRecorder()

        async def send(_client, request, **_kwargs):
            """Intercept final HTTP after LiteLLM has selected the provider."""

            return recorder.respond(request)

        environment = {
            "OLLAMA_MODEL": "qwen3:8b", "OLLAMA_BASE_URL": "http://ollama-contract.invalid:11434",
            "OPENAI_MODEL": "unused-openai", "OPENAI_API_KEY": "synthetic-unused-key",
            "OPENAI_BASE_URL": "https://must-not-call-openai.invalid/v1",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        }
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, environment, clear=True
        ), patch("httpx.AsyncClient.send", new=send), patch(
            "httpx.Client.send", side_effect=AssertionError("synchronous HTTP is forbidden")
        ), patch("litellm.telemetry", False):
            owner = OllamaModel.from_environment().build_for(framework)
            config = _authored_eval_config(Path(directory) / "test_config.json")
            score, follow_up, models = await _exercise_models(owner, framework, config)

        self.assertEqual(score, 1.0)
        self.assertEqual(follow_up, "Confirm Paris.")
        self.assertEqual(models, ("ollama_chat/qwen3:8b",) * 2)
        self.assertEqual(recorder.requests, [{
            "host": "ollama-contract.invalid", "path": "/api/chat",
            "model": "qwen3:8b", "bearer_auth": False,
        }] * 3)

    async def test_implicit_evaluator_endpoint_does_not_require_an_agent_binding(self):
        """Carry the configured endpoint even for agents with unrelated providers."""

        from google.adk.models.registry import LLMRegistry
        from google.adk.models.llm_request import LlmRequest
        from google.genai import types

        recorder = _CompletionRecorder()

        async def send(_client, request, **_kwargs):
            """Verify default transport configuration reaches native HTTP."""

            return recorder.respond(request)

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "OLLAMA_MODEL": "qwen3:8b", "OLLAMA_BASE_URL": "http://custom-ollama.invalid:11434",
            "OPENAI_API_KEY": "synthetic-unused-key",
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
            "host": "custom-ollama.invalid", "path": "/api/chat",
            "model": "qwen3:8b", "bearer_auth": False,
        }])
