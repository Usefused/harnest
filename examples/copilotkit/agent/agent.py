"""A deterministic Harnest agent for real CopilotKit interoperability checks."""

import asyncio

from harnest.agent import client_tool
from harnest.agent.approval import ApprovalDenied, request_human_approval
from harnest.graph import START, Edge, Event, Graph, GraphContext


@client_tool
async def set_accent(color: str) -> dict:
    """Change the demo accent in the browser and return the applied color."""

    raise AssertionError("The registered CopilotKit frontend tool must execute this")


def prepare(value: str, context: GraphContext) -> Event:
    """Publish a progress message and advance shared state once per user turn."""

    turns = int(context.state.get("turns", 0)) + 1
    return Event(
        output=value,
        message="Checking your request…",
        state_delta={"turns": turns, "last_request": value},
    )


async def publish() -> Event:
    """Require a real scoped approval before marking the fictional draft published."""

    try:
        async with request_human_approval(
            action="publish_demo_draft", message="Publish this fictional demo draft?",
        ):
            return Event(
                message="Demo draft published after your approval.",
                state_delta={"published": True},
            )
    except ApprovalDenied:
        return Event(message="Publication declined. No draft was published.")


async def respond(value: str) -> Event:
    """Exercise text, browser tools, approvals and errors without a model service."""

    await asyncio.sleep(0.35)
    command = value.strip().lower()
    if command == "theme":
        result = await set_accent(color="violet")
        return Event(message=f"Browser applied {result['color']}.", state_delta={"accent": result["color"]})
    if command == "approve":
        return await publish()
    if command == "error":
        raise ValueError("Intentional demo failure")
    if command == "slow":
        await asyncio.sleep(20)
        return Event(message="Slow request completed.")
    return Event(message=f"Harnest heard: {value}", output={"echo": value})


root_agent = Graph(
    name="copilotkit_demo",
    description="CopilotKit compatibility demo: chat, state, browser tools and approval.",
    nodes={"prepare": prepare, "respond": respond},
    edges=(Edge(START, "prepare"), Edge("prepare", "respond")),
)
