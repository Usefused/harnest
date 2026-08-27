def test_helpdesk_calls_triage_tool_for_blocked_production(smoke):
    response = smoke.respond(
        "Call triage_request exactly once for this synthetic issue: "
        "production is blocked because API authentication returns 401. "
        "Then report the queue and priority."
    )

    tool_results = [
        item for item in response["output"] if item["type"] == "tool_result"
    ]
    assert tool_results
    assert tool_results[0]["name"] == "triage_request"
    assert tool_results[0]["output"]["queue"] == "technical-support"
    assert tool_results[0]["output"]["priority"] == "urgent"


def test_helpdesk_stream_completes(smoke):
    events = smoke.stream("Reply with exactly: SMOKE STREAM WORKS")

    assert events[0]["type"] == "response.created"
    assert events[-1]["type"] == "response.completed"
    assert events[-1]["outputText"] == "SMOKE STREAM WORKS"
