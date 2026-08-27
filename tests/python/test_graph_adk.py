import asyncio
import unittest

from google.adk.apps import App
from google.adk.runners import InMemoryRunner
from google.adk.workflow import JoinNode, Workflow
from google.genai import types

from harnest.graph import START, Edge, Event, Graph, Join


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


class GraphAdkTests(unittest.TestCase):
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
