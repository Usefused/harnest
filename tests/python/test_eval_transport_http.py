"""Offline HTTP coverage for evals borrowing custom lifecycle authentication."""

import json
import os
import unittest
from unittest.mock import patch

import httpx

from harnest.eval_model_transport import eval_model_transports
from harnest.model import LiteLLMModel
from harnest.model_lifecycle import LiteLLMLifecycle, close_litellm_lifecycles


class _GatewayAuth(httpx.Auth):
    """Replace the OpenAI SDK placeholder credential with gateway authentication."""

    def auth_flow(self, request):
        """Apply only a synthetic custom header and never forward Bearer auth."""

        request.headers.pop("Authorization", None)
        request.headers["X-API-Key"] = "synthetic-gateway-credential"
        yield request


class _GatewayLifecycle(LiteLLMLifecycle):
    """Own one offline AsyncOpenAI client shared by agent, judge, and simulator."""

    def __init__(self):
        """Prepare sanitized request records and one response for each caller."""

        self.created = 0
        self.closed = 0
        self.transport = None
        self.requests = []
        self.borrowed_client_matches = []
        self._responses = iter(
            ("Paris.", '{"is_the_agent_response_valid": "valid"}', "Confirm Paris.")
        )

    async def create_transport(self, context):
        """Create one authenticated provider client without using environment keys."""

        from openai import AsyncOpenAI

        self.created += 1
        self.transport = AsyncOpenAI(
            api_key="synthetic-placeholder-never-forwarded",
            base_url="https://custom-gateway.invalid/v1",
            http_client=httpx.AsyncClient(
                auth=_GatewayAuth(), transport=httpx.MockTransport(self._respond)
            ),
        )
        return self.transport

    async def before_request(self, request, context):
        """Record identity only, never the client or its authentication values."""

        self.borrowed_client_matches.append(request["client"] is self.transport)
        return request

    async def close(self, context):
        """Close the sole owner-created provider client exactly once."""

        self.closed += 1
        await self.transport.close()

    def _respond(self, request):
        """Observe authenticated wire requests while retaining no credential text."""

        body = json.loads(request.content)
        self.requests.append(
            {
                "host": request.url.host,
                "path": request.url.path,
                "model": body["model"],
                "temperature": body.get("temperature"),
                "custom_auth": request.headers.get("X-API-Key")
                == "synthetic-gateway-credential",
                "bearer_auth": "Authorization" in request.headers,
            }
        )
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "chatcmpl-borrowed-transport",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": next(self._responses)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )


def _explicit_eval_config():
    """Keep distinct authored evaluator models and generation settings explicit."""

    from google.adk.evaluation.eval_config import EvalConfig

    return EvalConfig.model_validate(
        {
            "criteria": {
                "final_response_match_v2": {
                    "threshold": 1.0,
                    "judgeModelOptions": {
                        "judgeModel": "openai/gpt-4.1-mini",
                        "numSamples": 1,
                        "judgeModelConfig": {"temperature": 0.7},
                    },
                }
            },
            "userSimulatorConfig": {
                "model": "openai/gpt-4o-mini",
                "modelConfiguration": {"temperature": 0.3},
            },
        }
    )


async def _agent_response(owner, framework, content):
    """Use each framework's native agent call before constructing eval inputs."""

    from google.adk.models.llm_request import LlmRequest
    from google.genai import types

    if framework == "langgraph":
        # A real LangChain invocation exercises its owned controller directly;
        # the later judge/simulator calls cross the separate ADK borrowing bridge.
        from langchain_core.messages import HumanMessage

        response = await owner.ainvoke([HumanMessage(content="Which city?")])
        return types.Content(role="model", parts=[types.Part(text=response.content)])
    responses = [
        response
        async for response in owner.generate_content_async(
            LlmRequest(model=owner.model, contents=[content])
        )
    ]
    return responses[0].content


async def _exercise_models(owner, framework, config):
    """Invoke the native agent, real judge, and real simulator within one scope."""

    from google.adk.evaluation.conversation_scenarios import ConversationScenario
    from google.adk.evaluation.eval_case import Invocation
    from google.adk.evaluation.eval_config import get_eval_metrics_from_config
    from google.adk.evaluation.final_response_match_v2 import FinalResponseMatchV2Evaluator
    from google.adk.evaluation.simulation.llm_backed_user_simulator import (
        LlmBackedUserSimulator,
    )
    from google.genai import types

    content = types.Content(role="user", parts=[types.Part(text="Which city?")])
    response = await _agent_response(owner, framework, content)
    invocation = Invocation(user_content=content, final_response=response)
    with eval_model_transports(owner, config) as prepared:
        judge = FinalResponseMatchV2Evaluator(get_eval_metrics_from_config(prepared)[0])
        simulator = LlmBackedUserSimulator(
            config=prepared.user_simulator_config,
            conversation_scenario=ConversationScenario(
                starting_prompt="Which city?", conversation_plan="Confirm the answer."
            ),
        )
        judgment = await judge.evaluate_invocations([invocation], [invocation])
        await simulator.get_next_user_message([])
        follow_up = await simulator.get_next_user_message([])
        model_names = (judge._judge_model.model, simulator._llm.model)
    return judgment.overall_score, follow_up.user_message.parts[0].text, model_names


class EvalTransportHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def _assert_shared_gateway(self, framework):
        """Assert custom authentication and cleanup ownership across eval callers."""

        lifecycle = _GatewayLifecycle()
        # A missing binding must fail offline instead of falling back to a
        # developer's credentials or making a real provider network request.
        with patch.dict(
            os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}, clear=True
        ), patch(
            "httpx.AsyncHTTPTransport.handle_async_request",
            side_effect=AssertionError("unconfigured HTTP transport is forbidden"),
        ), patch(
            "httpx.Client.send", side_effect=AssertionError("sync HTTP is forbidden")
        ), patch("litellm.telemetry", False):
            owner = LiteLLMModel(
                "openai/gpt-4.1", lifecycle=lifecycle, temperature=0.1
            ).build_for(framework)
            try:
                score, follow_up, model_names = await _exercise_models(
                    owner, framework, _explicit_eval_config()
                )
                self.assertEqual(lifecycle.closed, 0)
            finally:
                await close_litellm_lifecycles(owner)
                await close_litellm_lifecycles(owner)

        self.assertEqual(score, 1.0)
        self.assertEqual(follow_up, "Confirm Paris.")
        self.assertEqual(model_names, ("openai/gpt-4.1-mini", "openai/gpt-4o-mini"))
        self.assertEqual(lifecycle.created, 1)
        self.assertEqual(lifecycle.closed, 1)
        self.assertEqual(lifecycle.borrowed_client_matches, [True, True, True])
        self.assertEqual(
            lifecycle.requests,
            [
                {
                    "host": "custom-gateway.invalid",
                    "path": "/v1/chat/completions",
                    "model": model,
                    "temperature": temperature,
                    "custom_auth": True,
                    "bearer_auth": False,
                }
                for model, temperature in (
                    ("gpt-4.1", 0.1), ("gpt-4.1-mini", 0.7), ("gpt-4o-mini", 0.3)
                )
            ],
        )

    async def test_evaluators_borrow_authenticated_http_client(self):
        """Every controller retains gateway auth and independent generation options."""

        for framework in ("adk", "langgraph"):
            with self.subTest(framework=framework):
                await self._assert_shared_gateway(framework)
