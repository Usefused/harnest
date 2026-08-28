import asyncio
import unittest
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.models import BaseLlm, LlmResponse
from google.adk.runners import InMemoryRunner
from google.adk.workflow import JoinNode, Workflow
from google.genai import types

from harnest.agent import Agent
from harnest.graph import START, Edge, Event, Graph, GraphContext, Join


async def _run(graph: Graph, message: str = "start"):
    workflow = graph.build()
    runner = InMemoryRunner(app=App(name=graph.name, root_agent=workflow))
    await runner.session_service.create_session(
        app_name=graph.name, user_id="test-user", session_id="test-session"
    )
    try:
        return [
            event
            async for event in runner.run_async(
                user_id="test-user",
                session_id="test-session",
                new_message=types.Content(
                    role="user", parts=[types.Part(text=message)]
                ),
            )
        ]
    finally:
        await runner.close()


class _RecordingLlm(BaseLlm):
    seen: list[list[str]] = []

    async def generate_content_async(self, request, stream=False):
        del stream
        self.seen.append(
            [
                part.text
                for content in request.contents
                for part in content.parts
                if part.text
            ]
        )
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part(text=f"reply-{len(self.seen)}")],
            )
        )


async def _run_conversation(history: str) -> tuple[str, list[list[str]]]:
    model = _RecordingLlm(model=f"recording-{history}")
    graph = Graph(
        name=f"{history}_conversation",
        nodes={
            "assistant": Agent(
                name="assistant",
                model=model,
                instruction="Remember the conversation.",
                history=history,
            )
        },
        edges=[Edge(START, "assistant")],
    )
    workflow = graph.build()
    native = next(node for node in workflow.graph.nodes if node.name == "assistant")
    runner = InMemoryRunner(app=App(name=graph.name, root_agent=workflow))
    await runner.session_service.create_session(
        app_name=graph.name, user_id="test-user", session_id="test-session"
    )
    try:
        for message in ("first", "second"):
            async for _ in runner.run_async(
                user_id="test-user",
                session_id="test-session",
                new_message=types.Content(
                    role="user", parts=[types.Part(text=message)]
                ),
            ):
                pass
    finally:
        await runner.close()
    return native.mode, model.seen


class GraphAdkTests(unittest.TestCase):
    def test_agent_history_controls_native_multi_turn_graph_context(self):
        session_mode, session_seen = asyncio.run(_run_conversation("session"))
        turn_mode, turn_seen = asyncio.run(_run_conversation("turn"))

        self.assertEqual(session_mode, "chat")
        self.assertEqual(session_seen[1], ["first", "reply-1", "second"])
        self.assertEqual(turn_mode, "single_turn")
        self.assertEqual(turn_seen[1], ["second"])

    def test_ir_validates_references_duplicates_and_reachability(self):
        with self.assertRaisesRegex(ValueError, "unknown target"):
            Graph(
                name="bad_ref",
                nodes={"one": lambda value: value},
                edges=[Edge(START, "missing")],
            )
        with self.assertRaisesRegex(ValueError, "duplicate edge"):
            Graph(
                name="duplicate",
                nodes={"one": lambda value: value},
                edges=[Edge(START, "one"), Edge(START, "one", route="again")],
            )
        with self.assertRaisesRegex(ValueError, "unreachable from START"):
            Graph(
                name="unreachable",
                nodes={"one": lambda value: value, "two": lambda value: value},
                edges=[Edge(START, "one")],
            )

    def test_lowers_join_nested_graph_and_route_lists(self):
        nested = Graph(
            name="nested",
            nodes={"inner": lambda value: "inner"},
            edges=[Edge(START, "inner")],
        )
        graph = Graph(
            name="composed",
            nodes={
                "left": lambda value: "left",
                "right": nested,
                "gather": Join(),
            },
            edges=[
                Edge(START, "left"),
                Edge(START, "right"),
                Edge("left", "gather", route=["a", "b"]),
                Edge("right", "gather"),
            ],
            max_concurrency=2,
        )

        workflow = graph.build()

        self.assertIsInstance(workflow, Workflow)
        self.assertEqual(workflow.max_concurrency, 2)
        self.assertIsInstance(
            next(node for node in workflow.graph.nodes if node.name == "gather"),
            JoinNode,
        )
        routed = next(
            edge
            for edge in workflow.graph.edges
            if edge.from_node.name == "left" and edge.to_node.name == "gather"
        )
        self.assertEqual(routed.route, ["a", "b"])

    def test_agent_advanced_embeds_native_node_and_rejects_root_boundaries(self):
        native = LlmAgent(name="native_agent", model="gemini-test")
        graph = Graph(
            name="hybrid",
            nodes={"native": Agent.advanced(native)},
            edges=[Edge(START, "native")],
        )

        workflow = graph.build()

        self.assertEqual(
            [node.name for node in workflow.graph.nodes if node.name != "__START__"],
            ["native"],
        )
        with self.assertRaisesRegex(ValueError, "root input/output adapters"):
            Graph(
                name="adapted",
                nodes={
                    "native": Agent.advanced(
                        LlmAgent(name="adapted_agent", model="gemini-test"),
                        output_adapter=lambda value: value,
                    )
                },
                edges=[Edge(START, "native")],
            ).build()
        with self.assertRaisesRegex(TypeError, "ADK App targets are root-only"):
            Graph(
                name="application_node",
                nodes={
                    "native": Agent.advanced(
                        App(
                            name="native_app",
                            root_agent=LlmAgent(
                                name="app_agent", model="gemini-test"
                            ),
                        )
                    )
                },
                edges=[Edge(START, "native")],
            ).build()

    def test_executes_deterministic_function_sequence(self):
        def first(node_input):
            return f"{node_input}-alpha"

        def second(node_input):
            return f"{node_input}-omega"

        graph = Graph(
            name="sequence",
            nodes={"first": first, "second": second},
            edges=[Edge(START, "first"), Edge("first", "second")],
        )

        events = asyncio.run(_run(graph))

        self.assertEqual(
            [event.output for event in events if event.output is not None],
            ["start-alpha", "start-alpha-omega"],
        )

    def test_callable_context_reads_and_persists_session_state(self):
        seen = []

        def remember(value: str, context: GraphContext) -> Event:
            count = int(context.state.get("count", 0)) + 1
            seen.append(dict(context.state))
            return Event(output=f"{value}:{count}", state_delta={"count": count})

        graph = Graph(
            name="stateful",
            nodes={"remember": remember},
            edges=[Edge(START, "remember")],
        )

        async def exercise() -> tuple[list[Any], dict[str, Any]]:
            workflow = graph.build()
            runner = InMemoryRunner(app=App(name=graph.name, root_agent=workflow))
            await runner.session_service.create_session(
                app_name=graph.name,
                user_id="test-user",
                session_id="test-session",
                state={"count": 2},
            )
            events = []
            try:
                async for event in runner.run_async(
                    user_id="test-user",
                    session_id="test-session",
                    new_message=types.Content(
                        role="user", parts=[types.Part(text="remember")]
                    ),
                ):
                    events.append(event)
                session = await runner.session_service.get_session(
                    app_name=graph.name,
                    user_id="test-user",
                    session_id="test-session",
                )
                return events, dict(session.state)
            finally:
                await runner.close()

        events, state = asyncio.run(exercise())

        self.assertEqual(seen, [{"count": 2}])
        self.assertEqual(state["count"], 3)
        self.assertEqual(
            [event.output for event in events if event.output is not None],
            ["remember:3"],
        )

    def test_executes_only_the_selected_routed_branch(self):
        calls = []

        def router(node_input):
            return Event(output="payload", route="selected", message="routing")

        def selected(node_input):
            calls.append("selected")
            return f"{node_input}-selected"

        def skipped(node_input):
            calls.append("skipped")
            return f"{node_input}-skipped"

        graph = Graph(
            name="routing",
            nodes={
                "router": router,
                "selected": selected,
                "skipped": skipped,
            },
            edges=[
                Edge(START, "router"),
                Edge("router", "selected", route="selected"),
                Edge("router", "skipped", route="skipped"),
            ],
        )

        events = asyncio.run(_run(graph))

        self.assertEqual(calls, ["selected"])
        self.assertEqual(
            [event.output for event in events if event.output is not None],
            ["payload", "payload-selected"],
        )
        self.assertTrue(
            any(
                part.text == "routing"
                for event in events
                for part in (event.content.parts if event.content else [])
            )
        )


if __name__ == "__main__":
    unittest.main()
