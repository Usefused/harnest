"""Classify with Jev, draft with an LLM, and preserve the validated routing result."""

from harnest.graph import START, Edge, Graph
from harnest.lib.triage import triage_ticket
from harnest.lib.reply import reply_node, finish_reply
from harnest.models.result import SupportResponse


root_agent = Graph(
    name="jev_triage",
    description="Use Jev to recommend a queue and an LLM to draft a helpful reply.",
    nodes={"triage": triage_ticket, "reply": reply_node(), "finish": finish_reply},
    edges=(Edge(START, "triage"), Edge("triage", "reply"), Edge("reply", "finish")),
    output_schema=SupportResponse,
)
