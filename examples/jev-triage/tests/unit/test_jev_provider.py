"""Exercise the actual optional SDK with in-memory HTTP responses, never live keys."""

import asyncio
import json

import pytest

from harnest.decisions import (
    Choice, DecisionDefinition, DecisionRequest, DecisionValidationError,
    DecisionBinding, Decisions,
)
from harnest.lib.decision_resources import decision_registry
from harnest.lib.definitions import TRIAGE, ROUTING
from harnest.lib.jev import JevProvider
from harnest.lib.triage import recommendation


sdk = pytest.importorskip("typesafe_sdk", reason="install the example's typesafe-sdk dependency")
httpx2 = pytest.importorskip("httpx2")


def response_body(department="billing", confidence=0.95):
    """Construct the SDK's native wire response with a complete choice distribution."""
    return {
        "model": JevProvider.version,
        "usage": {"input_tokens": 20, "output_tokens": 0},
        "answers": {"department": {
            "type": "choice", "choice": department, "confidence": confidence,
            "probabilities": {key: 1.0 if key == department else 0.0
                              for key in ("billing", "support", "other")},
        }},
    }


@pytest.mark.parametrize("department,confidence,action", [
    ("billing", 0.95, "route"), ("support", 0.9, "route"),
    ("billing", 0.3, "review"), ("other", 0.95, "review"),
])
def test_live_adapter_preserves_native_answers(department, confidence, action):
    """Validate request translation, model pinning and typed outcomes through the SDK."""
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(200, json=response_body(department, confidence))

    async def run():
        async with sdk.AsyncTypeSafeClient(api_key="test-only", transport=httpx2.MockTransport(handle)) as client:
            decisions = Decisions(providers={"jev": JevProvider(client)},
                                  bindings=(DecisionBinding(TRIAGE, "jev", ROUTING),))
            result = await decisions.evaluate("support_route", {"ticket": "private", "nested": {"items": [1, 2]}})
            return recommendation(result)

    result = asyncio.run(run())
    assert (result.action, result.department, result.confidence, result.mode) == (action, department, confidence, "live")
    assert len(requests) == 1
    assert requests[0]["model"] == "jev-1.13.0"
    assert requests[0]["state"]["nested"] == {"items": [1, 2]}
    assert requests[0]["questions"]["department"]["criteria"] == dict(TRIAGE.questions[0].options)


def test_resource_closes_client_and_does_not_retry_failures(monkeypatch):
    """Apply the live lifecycle settings instead of constructing a test-only policy."""
    requests = []
    clients = []
    real_client = sdk.AsyncTypeSafeClient

    def handle(request):
        requests.append(request)
        return httpx2.Response(503, json={"detail": "private upstream failure"})

    def client_factory(**options):
        assert options["api_key"] == "test-only"
        client = real_client(transport=httpx2.MockTransport(handle), **options)
        clients.append(client)
        return client

    async def run():
        async with decision_registry() as decisions:
            return recommendation(await decisions.evaluate("support_route", {"ticket": "private"}))

    monkeypatch.setenv("JEV_TRIAGE_OFFLINE", "false")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("TYPESAFE_AI_KEY", "test-only")
    monkeypatch.setattr(sdk, "AsyncTypeSafeClient", client_factory)
    result = asyncio.run(run())
    assert (result.action, result.queue, result.reason) == ("review", None, "evaluation_failed")
    assert result.error == "provider_error"
    assert "private" not in result.model_dump_json()
    assert len(requests) == 1
    assert clients[0]._http_client.is_closed


def test_unexpected_answers_are_not_dropped_by_the_adapter():
    """Preserve every returned key so the core can reject extra questions."""
    body = response_body()
    body["answers"]["extra"] = body["answers"]["department"]

    async def run():
        transport = httpx2.MockTransport(lambda request: httpx2.Response(200, json=body))
        async with sdk.AsyncTypeSafeClient(api_key="test-only", transport=transport) as client:
            decisions = Decisions(providers={"jev": JevProvider(client)},
                                  bindings=(DecisionBinding(TRIAGE, "jev", ROUTING),))
            await decisions.evaluate("support_route", {})

    with pytest.raises(DecisionValidationError):
        asyncio.run(run())


def test_batched_choices_use_one_request():
    """Keep independent questions together, as advertised by provider capabilities."""
    definition = DecisionDefinition("batch", "1", (
        Choice("first", "First?", {"billing": "Billing", "support": "Support", "other": "Other"}),
        Choice("second", "Second?", {"billing": "Billing", "support": "Support", "other": "Other"}),
    ))
    requests = []

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        body = response_body()
        answer = body["answers"]["department"]
        body["answers"] = {name: answer for name in payload["questions"]}
        return httpx2.Response(200, json=body)

    async def run():
        async with sdk.AsyncTypeSafeClient(api_key="test-only", transport=httpx2.MockTransport(handle)) as client:
            return await JevProvider(client).evaluate(DecisionRequest(definition, {"ticket": "test"}))

    response = asyncio.run(run())
    assert set(response.answers) == {"first", "second"}
    assert len(requests) == 1
