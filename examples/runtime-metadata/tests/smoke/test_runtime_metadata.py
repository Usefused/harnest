def _active_framework(result):
    """Return the one framework namespace populated by result validation."""

    active = {
        name: value
        for name, value in result["metadata"].items()
        if value is not None
    }
    assert len(active) == 1
    return next(iter(active.items()))


def _assert_native_turn(framework, metadata):
    """Check the native collection without normalizing its individual records."""

    key = "events" if framework == "adk" else "messages"
    assert framework in {"adk", "langgraph"}
    assert metadata[key]


def test_graph_result_contains_one_native_metadata_namespace(smoke, client):
    response = smoke.respond("metadata check")
    result = response["result"]

    assert result["answer"] == "Harnest received: metadata check"
    framework, metadata = _active_framework(result)
    _assert_native_turn(framework, metadata)

    transcript = client.get(f"/sessions/{response['sessionId']}/messages")
    assert transcript.status_code == 200
    messages = transcript.json()["messages"]
    assert messages
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["metadata"][framework]
