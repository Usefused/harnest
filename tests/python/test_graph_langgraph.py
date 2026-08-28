import importlib.util
import unittest
from unittest.mock import patch

from harnest.agent import Agent, AgentDefinition
from harnest.graph import START, Edge, Event, Graph, GraphContext, Join
from harnest.mcp import MCPClient


LANGGRAPH_AVAILABLE = importlib.util.find_spec("langgraph") is not None


@unittest.skipUnless(LANGGRAPH_AVAILABLE, "langgraph is not installed")
class LangGraphBackendTests(unittest.IsolatedAsyncioTestCase):
    def test_pydantic_output_schema_is_passed_as_response_format(self):
        from pydantic import BaseModel
        from harnest.backends.langgraph import build_agent

        class Answer(BaseModel):
            value: str

        definition = AgentDefinition(
            name="root",
            model="test:model",
            instruction="Answer.",
            output_schema=Answer,
        )
        with patch("langchain.agents.create_agent", return_value=object()) as create:
            build_agent(definition)

        self.assertIs(create.call_args.kwargs["response_format"], Answer)

    def test_runtime_metadata_is_not_provider_generated(self):
        from pydantic import BaseModel
        from harnest.backends.langgraph import build_agent
        from harnest.structured import FrameworkMetadata

        class Details(BaseModel):
            framework: str

        class Answer(BaseModel):
            value: str
            metadata: FrameworkMetadata[Details]

        definition = AgentDefinition(
            name="root",
            model="test:model",
            instruction="Answer.",
            output_schema=Answer,
        )
        with patch("langchain.agents.create_agent", return_value=object()) as create:
            build_agent(definition)

        provider_schema = create.call_args.kwargs["response_format"]
        self.assertNotIn("metadata", provider_schema.model_fields)

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

    async def test_agent_history_projects_session_or_current_turn(self):
        from langchain_core.messages import AIMessage, HumanMessage
        from langchain_core.runnables import RunnableLambda
        from harnest.backends.langgraph import build_graph

        messages = [
            HumanMessage(content="first"),
            AIMessage(content="reply"),
            HumanMessage(content="second"),
        ]
        for history, expected in (
            ("session", ["first", "reply", "second"]),
            ("turn", ["second"]),
        ):
            seen = []

            async def record(state):
                seen.append(state)
                return state

            graph = Graph(
                name=f"{history}_context",
                nodes={
                    "assistant": AgentDefinition(
                        name="assistant",
                        model="openai:test",
                        instruction="Remember.",
                        history=history,
                    )
                },
                edges=(Edge(START, "assistant"),),
            )
            with patch(
                "langchain.agents.create_agent",
                return_value=RunnableLambda(record),
            ):
                target = build_graph(graph)

            await target.ainvoke(
                {
                    "messages": messages,
                    "value": "second",
                    "_harnest_turn_start": 2,
                }
            )

            self.assertEqual(
                [message.content for message in seen[0]["messages"]], expected
            )
            self.assertNotIn("_harnest_turn_start", seen[0])

    async def test_agent_consumes_predecessor_output_with_selected_history(self):
        from langchain_core.messages import AIMessage, HumanMessage
        from langchain_core.runnables import RunnableLambda
        from harnest.backends.langgraph import build_graph

        messages = [
            HumanMessage(content="first"),
            AIMessage(content="reply"),
            HumanMessage(content="second"),
        ]
        for history, expected in (
            ("session", ["first", "reply", "second", "routed:second"]),
            ("turn", ["routed:second"]),
        ):
            seen = []

            async def record(state):
                seen.append(state)
                return state

            graph = Graph(
                name=f"{history}_routed_context",
                nodes={
                    "route": lambda value: f"routed:{value}",
                    "assistant": AgentDefinition(
                        name="assistant",
                        model="openai:test",
                        instruction="Answer the routed request.",
                        history=history,
                    ),
                },
                edges=(Edge(START, "route"), Edge("route", "assistant")),
            )
            with patch(
                "langchain.agents.create_agent",
                return_value=RunnableLambda(record),
            ):
                target = build_graph(graph)

            await target.ainvoke(
                {
                    "messages": messages,
                    "value": "second",
                    "_harnest_turn_start": 2,
                }
            )

            self.assertEqual(
                [message.content for message in seen[0]["messages"]],
                expected,
            )
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

    async def test_callable_context_reads_and_merges_session_state(self):
        from harnest.backends.langgraph import build_graph

        def remember(value: str, context: GraphContext) -> Event:
            count = int(context.state.get("count", 0)) + 1
            return Event(output=f"{value}:{count}", state_delta={"count": count})

        graph = Graph(
            name="stateful",
            nodes={"remember": remember},
            edges=(Edge(START, "remember"),),
        )

        result = await build_graph(graph).ainvoke(
            {
                "value": "remember",
                "messages": [],
                "_harnest_state": {"count": 2, "tenant": "one"},
            }
        )

        self.assertEqual(result["value"], "remember:3")
        self.assertEqual(result["_harnest_state"], {"count": 3, "tenant": "one"})

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

    async def test_agent_advanced_can_embed_native_pregel_as_a_graph_node(self):
        from harnest.backends.langgraph import build_graph
        from langgraph.graph import END, START as LANGGRAPH_START, StateGraph

        visited = []

        def native_step(state):
            visited.append(state["value"])
            return state

        builder = StateGraph(dict)
        builder.add_node("native_step", native_step)
        builder.add_edge(LANGGRAPH_START, "native_step")
        builder.add_edge("native_step", END)
        native = builder.compile()
        graph = Graph(
            name="hybrid_graph",
            nodes={"advanced_node": Agent.advanced(native)},
            edges=(Edge(START, "advanced_node"),),
        )

        result = await build_graph(graph).ainvoke(
            {"value": "hello", "messages": []},
            config={"configurable": {"thread_id": "hybrid"}},
        )

        self.assertEqual(visited, ["hello"])
        self.assertEqual(result["value"], "hello")

    async def test_embedded_advanced_pregel_rejects_root_adapters(self):
        from harnest.backends.langgraph import build_graph
        from langgraph.graph import END, START as LANGGRAPH_START, StateGraph

        builder = StateGraph(dict)
        builder.add_node("identity", lambda state: state)
        builder.add_edge(LANGGRAPH_START, "identity")
        builder.add_edge("identity", END)
        native = builder.compile()
        graph = Graph(
            name="adapted_graph",
            nodes={
                "advanced_node": Agent.advanced(
                    native, output_adapter=lambda value: value
                )
            },
            edges=(Edge(START, "advanced_node"),),
        )

        with self.assertRaisesRegex(ValueError, "root input/output adapters"):
            build_graph(graph)


if __name__ == "__main__":
    unittest.main()
