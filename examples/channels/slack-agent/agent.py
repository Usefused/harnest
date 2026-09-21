"""Deterministic channel acceptance agent; no model or external tool credentials."""

from harnest.graph import START, Edge, Event, Graph


async def respond(message):
    """Return a recognizable answer through the real compiled agent runtime."""
    answer = "Harnest received your test mention. The shared Fused connection is working."
    return Event(output=answer, message=answer)


root_agent = Graph(name="slack_channel_test", nodes={"respond": respond}, edges=(Edge(START, "respond"),))
