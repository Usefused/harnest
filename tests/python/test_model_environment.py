"""Offline adapter coverage for Harnest's canonical model environment."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from harnest.bundle import EvalSuite
from harnest.evaluation import eval_config
from harnest.model import LiteLLMModel


class _CompletionRecorder:
    """Return offline completions while retaining only sanitized wire metadata."""

    def __init__(self, expected_key):
        """Keep the synthetic credential private and prepare one reply per lane."""

        self._expected_key = expected_key
        self.requests = []
        self._replies = iter(
            (
                "Paris.",
                '{"is_the_agent_response_valid": "valid"}',
                "Please confirm the destination.",
            )
        )

    def respond(self, request):
        """Inspect the real OpenAI request without recording its credential."""

        payload = json.loads(request.content)
        self.requests.append(
            {
                "method": request.method,
                "host": request.url.host,
                "path": request.url.path,
                "model": payload["model"],
                "payloadKeys": sorted(payload),
                # A boolean is sufficient evidence of credential routing and
                # cannot disclose the header through assertion diagnostics.
                "authenticated": request.headers.get("authorization")
                == f"Bearer {self._expected_key}",
            }
        )
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "chatcmpl-offline-contract",
                "object": "chat.completion",
                "created": 1,
                "model": payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": next(self._replies),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )


def _authored_eval_config(path):
    """Omit judge and simulator models so the shared environment supplies both."""

    path.write_text(
        json.dumps(
            {
                "criteria": {
                    "final_response_match_v2": {
                        "threshold": 1.0,
                        "judgeModelOptions": {"numSamples": 1},
                    }
                },
                "userSimulatorConfig": {},
            }
        ),
        encoding="utf-8",
    )
    return eval_config(EvalSuite((), path), "business")


class ModelEnvironmentAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_judge_and_simulator_share_openai_http_contract(self):
        """Exercise all three real ADK adapters through an offline HTTP boundary."""

        synthetic_key = "synthetic-openai-contract-key"
        recorder = _CompletionRecorder(synthetic_key)

        async def send(_client, request, **_kwargs):
            """Intercept the final HTTP request after provider configuration."""

            return recorder.respond(request)

        environment = {
            "OPENAI_MODEL": "gpt-4.1",
            "OPENAI_API_KEY": synthetic_key,
            "OPENAI_BASE_URL": "https://openai-contract.invalid/v1",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        }
        # Clearing the environment removes real credentials and OLLAMA_API_KEY;
        # neither a developer's shell nor native-provider defaults may help pass.
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, environment, clear=True
        ), patch("httpx.AsyncClient.send", new=send), patch(
            "httpx.Client.send",
            side_effect=AssertionError("synchronous HTTP is not allowed"),
        ), patch("litellm.telemetry", False):
            from google.adk.evaluation.conversation_scenarios import ConversationScenario
            from google.adk.evaluation.eval_case import Invocation
            from google.adk.evaluation.eval_config import get_eval_metrics_from_config
            from google.adk.evaluation.final_response_match_v2 import (
                FinalResponseMatchV2Evaluator,
            )
            from google.adk.evaluation.simulation.llm_backed_user_simulator import (
                LlmBackedUserSimulator,
            )
            from google.adk.models.llm_request import LlmRequest
            from google.genai import types

            agent = LiteLLMModel.from_openai_environment().build()
            config = _authored_eval_config(Path(directory) / "test_config.json")
            judge = FinalResponseMatchV2Evaluator(
                get_eval_metrics_from_config(config)[0]
            )
            simulator = LlmBackedUserSimulator(
                config=config.user_simulator_config,
                conversation_scenario=ConversationScenario(
                    starting_prompt="Which city?",
                    conversation_plan="Ask for confirmation after the answer.",
                ),
            )
            # Exercise ADK's actual simulator default, including Gemini-style
            # thinking hints, instead of hiding it behind an empty override.
            thinking = (
                config.user_simulator_config.model_configuration.thinking_config
            )
            self.assertEqual(thinking.thinking_budget, 10240)
            content = types.Content(
                role="user", parts=[types.Part(text="Which city?")]
            )
            responses = [
                response
                async for response in agent.generate_content_async(
                    LlmRequest(model=agent.model, contents=[content])
                )
            ]
            invocation = Invocation(
                user_content=content,
                final_response=responses[0].content,
            )
            judgment = await judge.evaluate_invocations([invocation], [invocation])
            # The first simulator turn is authored text; its second turn must
            # invoke the actual configured model to exercise the HTTP adapter.
            await simulator.get_next_user_message([])
            follow_up = await simulator.get_next_user_message([])

        self.assertEqual(judgment.overall_score, 1.0)
        self.assertEqual(
            follow_up.user_message.parts[0].text,
            "Please confirm the destination.",
        )
        # Exact payload keys prove the simulator's Gemini thinking defaults do
        # not leak provider-specific options into the OpenAI chat request.
        self.assertEqual(
            recorder.requests,
            [
                {
                    "method": "POST",
                    "host": "openai-contract.invalid",
                    "path": "/v1/chat/completions",
                    "model": "gpt-4.1",
                    "payloadKeys": ["messages", "model"],
                    "authenticated": True,
                }
            ]
            * 3,
        )
