"""Deterministic agent application for exercising runtime-owned metadata."""

from harnest.graph import START, Edge, Event, Graph
from harnest.models.result import MetadataResult


def respond(value: str) -> Event:
    """Return provider-independent output so only runtime enrichment is tested."""

    return Event(
        output={"answer": f"Harnest received: {value}"},
        message=f"Harnest received: {value}",
    )


root_agent = Graph(
    name="runtime_metadata_demo",
    description="Demonstrates typed native turn metadata on graph output.",
    nodes={"respond": respond},
    edges=(Edge(START, "respond"),),
    output_schema=MetadataResult,
)
