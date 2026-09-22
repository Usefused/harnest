"""The served agent uses a labeled fixture and needs no live provider credentials."""


def test_served_ticket_returns_a_typed_offline_recommendation(smoke):
    """Exercise the compiler, lifecycle, graph and structured response boundary."""
    response = smoke.respond("I was charged twice for the same order.")
    assert response["result"] == {
        "reply": "Offline fixture — Your ticket is recommended for the billing queue. No ticket has been sent.",
    }
    assert not any(item["type"] == "decision_result" for item in response["output"])
