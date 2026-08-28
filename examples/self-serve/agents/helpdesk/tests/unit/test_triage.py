def test_compiled_agent_contains_technical_specialist(agent):
    assert agent.name == "helpdesk"
    assert [child.name for child in agent.sub_agents] == ["technical_specialist"]


def test_production_api_issue_is_urgent_technical_support(tools):
    result = tools["triage_request"](
        "Fictional demo API authentication outage",
        production_blocked=True,
    )

    assert result.model_dump() == {
        "queue": "technical-support",
        "priority": "urgent",
        "reason": "Production is blocked.",
    }
