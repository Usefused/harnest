"""Offline decisions exercise the exact policy and output contract used live."""

import asyncio

import pytest

from harnest.decisions import (
    ChoiceResult, DecisionBinding, DecisionResponse, Decisions, FixtureDecisionProvider,
)
from harnest.lib.decision_resources import offline_enabled
from harnest.lib.definitions import TRIAGE, ROUTING
from harnest.lib.jev import JevProvider
from harnest.lib.triage import recommendation
from harnest.lib.reply import finish_reply, offline_reply, reply_node
from harnest.graph import GraphContext
from harnest.agent import Agent


def test_compiled_agent_identity(agent):
    assert agent.name == "jev_triage"


@pytest.mark.parametrize("department,confidence,action,reason", [
    ("billing", 0.95, "route", "classified"),
    ("support", 0.9, "route", "classified"),
    ("billing", 0.79, "review", "low_confidence"),
    ("support", 0.8, "route", "classified"),
    ("other", 0.95, "review", "other"),
])
def test_policy_recommendations(department, confidence, action, reason):
    """Apply thresholds to validated fixtures rather than duplicating policy in tests."""
    probabilities = {name: 1.0 if name == department else 0.0 for name in TRIAGE.questions[0].options}
    fixture = FixtureDecisionProvider({("support_route", "1"): DecisionResponse({
        "department": ChoiceResult(department, probabilities, confidence),
    })}, capabilities=JevProvider.capabilities)
    decisions = Decisions(providers={"fixture": fixture}, bindings=(DecisionBinding(TRIAGE, "fixture", ROUTING),))
    result = recommendation(asyncio.run(decisions.evaluate("support_route", {"ticket": "private"})))
    assert (result.action, result.reason, result.mode) == (action, reason, "offline")
    assert result.queue == (department if action == "route" else None)
    assert "private" not in result.model_dump_json()


def test_invalid_offline_mode_fails(monkeypatch):
    """Do not let a misspelled fixture flag accidentally enable network calls."""
    monkeypatch.setenv("JEV_TRIAGE_OFFLINE", "yes")
    with pytest.raises(ValueError, match="true or false"):
        offline_enabled()


def test_reply_keeps_review_decision_authoritative():
    """LLM text cannot replace the routing fields saved by the decision step."""
    decision = {
        "action": "review", "queue": None, "department": "billing", "confidence": 0.3,
        "reason": "low_confidence", "mode": "live", "provider": "jev",
        "provider_version": JevProvider.version, "error": None,
    }
    draft = '{"action":"route","queue":"support"}'
    state = {"jev_triage_decision": decision}
    event = finish_reply(draft, GraphContext(state))
    assert event.output == {"reply": draft}
    assert state["jev_triage_decision"] == decision
    assert "manual review" in offline_reply({"decision": decision})
    with pytest.raises(ValueError, match="non-empty"):
        finish_reply("  ", GraphContext({"jev_triage_decision": decision}))


def test_live_reply_uses_configurable_model(monkeypatch):
    """Explicit model settings reach the managed node without a network connection."""
    monkeypatch.setenv("JEV_TRIAGE_OFFLINE", "false")
    monkeypatch.setenv("JEV_LLM_MODEL", "openai/custom-model")
    monkeypatch.setenv("JEV_LLM_API_BASE", "https://models.example.invalid/v1")
    monkeypatch.setenv("JEV_LLM_API_KEY", "test-only")
    monkeypatch.setenv("JEV_LLM_REASONING_EFFORT", "none")
    node = reply_node()
    assert isinstance(node, Agent)
    assert node.history == "turn"
    assert node.model.model == "openai/custom-model"
    assert node.model.completion_args["api_key"] == "test-only"
    assert node.model.completion_args["api_base"] == "https://models.example.invalid/v1"
    assert node.model.completion_args["reasoning_effort"] == "none"
