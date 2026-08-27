import importlib.util
import unittest
from unittest.mock import patch

from harnest.agent import AgentDefinition
from harnest.graph import START, Edge, Event, Graph, Join
from harnest.mcp import MCPClient


LANGGRAPH_AVAILABLE = importlib.util.find_spec("langgraph") is not None


@unittest.skipUnless(LANGGRAPH_AVAILABLE, "langgraph is not installed")
class LangGraphBackendTests(unittest.IsolatedAsyncioTestCase):
    def test_native_middleware_is_passed_to_create_agent(self):
        from langchain.agents.middleware import AgentMiddleware
        from harnest.agent import AgentDefinition
        from harnest.backends.langgraph import build_agent

        definition = AgentDefinition(
            name="root", model="test:model", instruction="Answer."
        )
        middleware = AgentMiddleware()
        with patch("langchain.agents.create_agent", return_value=object()) as create:
            build_agent(definition, middleware=(middleware,))

        self.assertEqual(create.call_args.kwargs["middleware"], [middleware])
    async def test_mcp_agent_lowers_to_inert_runtime_plan(self):
        from harnest.backends.langgraph import ManagedAgentPlan, build_agent

        definition = AgentDefinition(
            name="portable",
            model="openai:test",
            instruction="Use the available tools.",
            mcp=(MCPClient.sse("https://mcp.example/sse"),),
        )

        plan = build_agent(definition, tools=("local-tool",))

        self.assertIsInstance(plan, ManagedAgentPlan)
        self.assertIs(plan.definition, definition)
        self.assertEqual(plan.tools, ("local-tool",))
        self.assertFalse(hasattr(plan, "ainvoke"))
        self.assertFalse(hasattr(plan, "astream"))

    async def test_graph_with_mcp_agent_lowers_to_inert_runtime_plan(self):
        from harnest.backends.langgraph import ManagedGraphPlan, build_graph

        agent = AgentDefinition(
            name="nested",
            model="openai:test",
            instruction="Use the available tools.",
            mcp=(MCPClient.sse("https://mcp.example/sse"),),
        )
        graph = Graph(
            name="nested_mcp",
            nodes={"agent": agent},
            edges=(Edge(START, "agent"),),
        )

        plan = build_graph(graph)

        self.assertIsInstance(plan, ManagedGraphPlan)
        self.assertIs(plan.graph, graph)
        self.assertEqual(plan.name, "nested_mcp")
        self.assertFalse(hasattr(plan, "ainvoke"))

    async def test_build_agent_does_not_create_hidden_checkpoint_state(self):
        from harnest.backends.langgraph import build_agent

        sentinel = object()
        with patch("langchain.agents.create_agent", return_value=sentinel) as create:
            result = build_agent(
                AgentDefinition(
                    name="portable",
                    model="openai:test",
                    instruction="Respond deterministically.",
                )
            )

        self.assertIs(result, sentinel)
        self.assertNotIn("checkpointer", create.call_args.kwargs)

    async def test_build_graph_runs_deterministic_sequence(self):
        from harnest.backends.langgraph import build_graph

        def double(value: int) -> int:
            return value * 2

        def increment(value: int) -> Event:
            return Event(output=value + 1, message="sequence complete")

        graph = Graph(
            name="sequence",
            nodes={"double": double, "increment": increment},
            edges=(Edge(START, "double"), Edge("double", "increment")),
        )

        compiled = build_graph(graph)
        result = await compiled.ainvoke(
            {"value": 3, "messages": []},
            config={"configurable": {"thread_id": "sequence"}},
        )

        self.assertIsNone(compiled.checkpointer)
        self.assertEqual(result["value"], 7)
        self.assertEqual(result["messages"][-1].content, "sequence complete")

    async def test_build_graph_selects_only_matching_routed_branch(self):
        from harnest.backends.langgraph import build_graph

        visited: list[str] = []

        def classify(value: int) -> Event:
            return Event(output=value, route="high" if value >= 10 else "low")

        def high(value: int) -> str:
            visited.append("high")
            return f"high:{value}"

        def low(value: int) -> str:
            visited.append("low")
            return f"low:{value}"

        graph = Graph(
            name="routed",
            nodes={"classify": classify, "high": high, "low": low},
            edges=(
                Edge(START, "classify"),
                Edge("classify", "high", route="high"),
                Edge("classify", "low", route="low"),
            ),
        )

        result = await build_graph(graph).ainvoke(
            {"value": 12, "messages": []},
            config={"configurable": {"thread_id": "routed"}},
        )

        self.assertEqual(result["value"], "high:12")
        self.assertEqual(visited, ["high"])

    async def test_parallel_branches_rejoin_without_state_write_conflicts(self):
        from harnest.backends.langgraph import build_graph

        visited: list[str] = []

        def left(value: str) -> str:
            visited.append("left")
            return f"{value}-left"

        def right(value: str) -> str:
            visited.append("right")
            return f"{value}-right"

        def after(value: str) -> str:
            visited.append("after")
            return f"{value}-done"

        graph = Graph(
            name="parallel_join",
            nodes={"left": left, "right": right, "join": Join(), "after": after},
            edges=(
                Edge(START, "left"),
                Edge(START, "right"),
                Edge("left", "join"),
                Edge("right", "join"),
                Edge("join", "after"),
            ),
        )

        result = await build_graph(graph).ainvoke(
            {"value": "start", "messages": []},
            config={"configurable": {"thread_id": "parallel-join"}},
        )

        self.assertCountEqual(visited[:2], ["left", "right"])
        self.assertEqual(visited[2:], ["after"])
        self.assertIn(
            result["value"],
            {"start-left-done", "start-right-done"},
        )

    async def test_native_pregel_is_validated_and_passed_through(self):
        from harnest.backends.langgraph import build_graph
        from langgraph.graph import END, START as LANGGRAPH_START, StateGraph

        builder = StateGraph(dict)
        builder.add_node("identity", lambda state: state)
        builder.add_edge(LANGGRAPH_START, "identity")
        builder.add_edge("identity", END)
        native = builder.compile()

        self.assertIs(build_graph(native), native)


if __name__ == "__main__":
    unittest.main()
