import sys
import unittest
from dataclasses import replace
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import httpx

from harnest.agent import Agent, AgentDefinition
from harnest.approval import ApprovalPolicy
from harnest.application import CompiledApplication
from harnest.backends.langgraph import ManagedAgentPlan, ManagedGraphPlan
from harnest.graph import START, Edge, Graph
from harnest.mcp import MCPClient
from harnest.mcp_lifecycle import (
    MCPClientContext,
    MCPClientLifecycle,
    MCPHTTPClientOptions,
)
from harnest.neutral_runtime import InvocationRequest, SessionConflictError
from harnest.output import OutputPolicy
from harnest.runtime_langgraph import LangGraphRuntimeDriver
from harnest.session import InMemorySessionStore


class _RecordingSessionStore(InMemorySessionStore):
    def __init__(self):
        super().__init__()
        self.closed = False

    async def close(self):
        self.closed = True
        await super().close()


class _Message:
    def __init__(
        self,
        content,
        *,
        message_type="ai",
        tool_calls=(),
        tool_call_id=None,
        name=None,
    ):
        self.content = content
        self.type = message_type
        self.tool_calls = list(tool_calls)
        self.tool_call_id = tool_call_id
        self.name = name

    def model_dump(self, **_kwargs):
        return {
            "type": self.type,
            "content": self.content,
            "tool_calls": self.tool_calls,
            "tool_call_id": self.tool_call_id,
            "name": self.name,
        }


class _HumanMessage:
    type = "human"

    def __init__(self, content):
        self.content = content

    def model_dump(self, **_kwargs):
        return {"type": self.type, "content": self.content}


class _Target:
    def __init__(self):
        self.inputs = []
        self.configs = []
        self.closed = 0

    def _result(self, graph_input):
        counter = graph_input.get("counter", 0) + 1
        return {
            "messages": [
                _Message(
                    "",
                    tool_calls=[
                        {"id": "call-1", "name": "echo", "args": {"text": "ok"}}
                    ],
                ),
                _Message(
                    "ok",
                    message_type="tool",
                    tool_call_id="call-1",
                    name="echo",
                ),
                _Message(
                    [
                        {"type": "thinking", "thinking": "private"},
                        {"type": "text", "text": f"answer-{counter}"},
                    ]
                ),
            ],
            "counter": counter,
            "value": f"value-{counter}",
        }

    async def ainvoke(self, graph_input, *, config):
        self.inputs.append(graph_input)
        self.configs.append(config)
        return self._result(graph_input)

    async def astream(self, graph_input, *, config, stream_mode):
        self.inputs.append(graph_input)
        self.configs.append(config)
        assert stream_mode == ["messages", "values"]
        result = self._result(graph_input)
        yield "messages", (_Message("answer-"), {"node": "reply"})
        yield "messages", (result["messages"][0], {"node": "reply"})
        yield "messages", (result["messages"][1], {"node": "tools"})
        yield "messages", (_Message(str(result["counter"])), {"node": "reply"})
        yield "values", result

    async def aclose(self):
        self.closed += 1


class _MCPAdapterClient:
    instances = []

    def __init__(self, connections, *, tool_interceptors, tool_name_prefix):
        self.connections = connections
        self.tool_interceptors = tool_interceptors
        self.tool_name_prefix = tool_name_prefix
        self.requests = []
        self.http_clients = []
        self.closed = 0
        self.__class__.instances.append(self)

    async def get_tools(self, *, server_name):
        self.requests.append(server_name)
        factory = self.connections[server_name].get("httpx_client_factory")
        if factory is not None:
            self.http_clients.append(
                factory(headers={"X-Adapter": "langgraph"}, timeout=1.0)
            )
        return [
            SimpleNamespace(name=f"{server_name}_echo"),
            SimpleNamespace(name=f"{server_name}_hidden"),
        ]

    async def aclose(self):
        for client in self.http_clients:
            await client.aclose()
        self.closed += 1


class _TrackedHTTPClient(httpx.AsyncClient):
    """Expose adapter-owned session cleanup ordering to the runtime test."""

    def __init__(self, events, **kwargs):
        super().__init__(**kwargs)
        self.events = events

    async def aclose(self):
        self.events.append("session-close")
        await super().aclose()


class _RuntimeMCPLifecycle(MCPClientLifecycle):
    """Record the application and session ownership boundaries."""

    def __init__(self):
        self.events = []

    async def start(self, context: MCPClientContext):
        self.events.append(f"start:{context.framework}")

    def create_http_client(
        self, options: MCPHTTPClientOptions, context: MCPClientContext
    ):
        self.events.append(f"create:{context.framework}")
        return _TrackedHTTPClient(
            self.events,
            headers=dict(options.headers),
            timeout=options.timeout,
            auth=options.auth,
        )

    async def close(self, context: MCPClientContext):
        self.events.append(f"lifecycle-close:{context.framework}")


def _request(
    session_id="session-1", *, state_delta=None, invocation_id="invocation-1"
):
    return InvocationRequest(
        input="hello",
        user_id="user-1",
        session_id=session_id,
        invocation_id=invocation_id,
        metadata={"source": "test"},
        state_delta=state_delta or {},
    )


def _application(target, *, bridge=None, kind=None):
    return CompiledApplication(
        name="portable",
        framework="langgraph",
        mode="advanced" if bridge is not None else "managed",
        target=target,
        kind=kind or ("advanced" if bridge is not None else "agent"),
        bridge=bridge,
    )


class LangGraphRuntimeDriverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _MCPAdapterClient.instances.clear()
        langchain_core = ModuleType("langchain_core")
        langchain_core.__path__ = []
        messages = ModuleType("langchain_core.messages")
        messages.HumanMessage = _HumanMessage
        self.langchain_modules = patch.dict(
            sys.modules,
            {
                "langchain_core": langchain_core,
                "langchain_core.messages": messages,
            },
        )
        self.langchain_modules.start()
        self.target = _Target()
        self.driver = LangGraphRuntimeDriver(
            _application(self.target),
            card={"name": "Portable agent", "description": "Deterministic"},
        )

    async def asyncTearDown(self):
        await self.driver.close()
        self.langchain_modules.stop()

    async def test_session_map_is_user_scoped_and_supports_crud(self):
        created = await self.driver.create_session(
            session_id="session-1", user_id="user-1", state={"counter": 1}
        )
        await self.driver.create_session(
            session_id="session-2", user_id="user-1", state={}
        )
        await self.driver.create_session(
            session_id="session-1", user_id="user-2", state={"private": True}
        )

        self.assertEqual(created.state, {"counter": 1})
        self.assertEqual(self.driver.info.framework, "langgraph")
        self.assertEqual(self.driver.info.name, "Portable agent")
        self.assertEqual(
            [item.id for item in await self.driver.list_sessions(user_id="user-1")],
            ["session-1", "session-2"],
        )
        self.assertIsNone(
            await self.driver.get_session(
                session_id="session-2", user_id="another-user"
            )
        )

        updated = await self.driver.update_session(
            session_id="session-1",
            user_id="user-1",
            state_delta={"enabled": True},
        )
        self.assertEqual(updated.state, {"counter": 1, "enabled": True})
        self.assertTrue(
            await self.driver.delete_session(
                session_id="session-2", user_id="user-1"
            )
        )
        self.assertFalse(
            await self.driver.delete_session(
                session_id="session-2", user_id="user-1"
            )
        )
        with self.assertRaises(SessionConflictError):
            await self.driver.create_session(
                session_id="session-1", user_id="user-1", state={}
            )

    async def test_invoke_normalizes_events_and_persists_returned_state(self):
        await self.driver.create_session(
            session_id="session-1", user_id="user-1", state={"counter": 1}
        )

        result = await self.driver.invoke(
            _request(state_delta={"request_flag": "present"})
        )

        self.assertEqual(result.text, "answer-2")
        self.assertEqual(result.session_id, "session-1")
        self.assertEqual(result.metadata, {"source": "test"})
        self.assertEqual(
            [event["type"] for event in result.events],
            ["tool_call", "tool_result", "message", "graph_output"],
        )
        self.assertEqual(result.events[0]["arguments"], {"text": "ok"})
        self.assertEqual(result.events[1]["result"], "ok")
        self.assertEqual(result.events[2]["text"], "answer-2")
        self.assertNotIn("private", result.events[2]["text"])
        self.assertEqual(result.result["counter"], 2)
        self.assertEqual(self.target.inputs[0]["request_flag"], "present")

        session = await self.driver.get_session(
            session_id="session-1", user_id="user-1"
        )
        self.assertEqual(session.state["counter"], 2)
        self.assertNotIn("messages", session.state)
        self.assertIn("messages", session.metadata["langgraph"])
        self.assertEqual(
            session.metadata["langgraph"]["messages"][-1]["content"][1]["text"],
            "answer-2",
        )
        messages = await self.driver.get_session_messages(
            session_id="session-1", user_id="user-1"
        )
        self.assertEqual(
            [message.role for message in messages],
            ["assistant", "tool", "assistant"],
        )
        self.assertEqual(messages[-1].content, "answer-2")
        self.assertIn("langgraph", messages[-1].metadata)
        self.assertIsNone(
            await self.driver.get_session_messages(
                session_id="missing", user_id="user-1"
            )
        )

        await self.driver.invoke(_request(invocation_id="invocation-2"))
        thread_ids = [item["configurable"]["thread_id"] for item in self.target.configs]
        self.assertEqual(len(set(thread_ids)), 2)
        self.assertEqual(thread_ids, ["invocation-1", "invocation-2"])
        self.assertNotIn("session-1", thread_ids)
        self.assertEqual(len(self.target.inputs[1]["messages"]), 4)

    async def test_managed_graph_keeps_session_state_public_and_namespaced(self):
        class StatefulGraphTarget(_Target):
            def _result(self, graph_input):
                state = dict(graph_input["_harnest_state"])
                state["count"] = state.get("count", 0) + 1
                return {
                    **graph_input,
                    "_harnest_state": state,
                    "value": f"count:{state['count']}",
                }

        target = StatefulGraphTarget()
        driver = LangGraphRuntimeDriver(_application(target, kind="graph"))
        await driver.create_session(
            session_id="graph-session",
            user_id="user-1",
            state={"count": 2, "tenant": "one"},
        )

        result = await driver.invoke(_request("graph-session"))
        session = await driver.get_session(
            session_id="graph-session", user_id="user-1"
        )

        self.assertEqual(result.result["value"], "count:3")
        self.assertEqual(session.state, {"count": 3, "tenant": "one"})
        self.assertIn("messages", session.metadata["langgraph"])
        self.assertEqual(
            session.metadata["langgraph"]["_harnest_state"],
            {"count": 3, "tenant": "one"},
        )
        self.assertEqual(
            target.inputs[0]["_harnest_state"], {"count": 2, "tenant": "one"}
        )
        await driver.close()

    async def test_stream_emits_only_canonical_events_and_updates_state(self):
        await self.driver.create_session(
            session_id="session-1", user_id="user-1", state={"counter": 3}
        )

        events = [event async for event in self.driver.stream(_request())]

        self.assertEqual(
            [event["type"] for event in events],
            ["message", "tool_call", "tool_result", "message", "graph_output"],
        )
        self.assertEqual(
            "".join(event["text"] for event in events if event["type"] == "message"),
            "answer-4",
        )
        self.assertEqual(events[-1]["output"]["counter"], 4)
        session = await self.driver.get_session(
            session_id="session-1", user_id="user-1"
        )
        self.assertEqual(session.state["counter"], 4)

    async def test_pre_tool_narration_is_suppressed_by_default_and_can_be_included(self):
        class NarratingTarget(_Target):
            def _result(self, graph_input):
                return {
                    "messages": [
                        *graph_input.get("messages", ()),
                        _Message(
                            "I'll inspect. ",
                            tool_calls=[
                                {
                                    "id": "call-1",
                                    "name": "inspect",
                                    "args": {"selector": "main"},
                                }
                            ],
                        ),
                        _Message(
                            "highlighted",
                            message_type="tool",
                            tool_call_id="call-1",
                            name="inspect",
                        ),
                        _Message("Done."),
                    ],
                    "value": "Done.",
                }

            async def astream(self, graph_input, *, config, stream_mode):
                self.inputs.append(graph_input)
                self.configs.append(config)
                result = self._result(graph_input)
                for message in result["messages"][-3:]:
                    yield "messages", (message, {"node": "researcher"})
                yield "values", result

        async def run(policy):
            target = NarratingTarget()
            application = replace(_application(target), output_policy=policy)
            driver = LangGraphRuntimeDriver(application)
            await driver.create_session(
                session_id="session-1", user_id="user-1", state={}
            )
            invoked = await driver.invoke(_request())
            streamed = [
                event
                async for event in driver.stream(
                    _request(invocation_id="invocation-2")
                )
            ]
            await driver.close()
            return invoked, streamed

        suppressed, suppressed_stream = await run(OutputPolicy())
        included, included_stream = await run(
            OutputPolicy(subagent_messages="include")
        )

        self.assertEqual(suppressed.text, "Done.")
        self.assertNotIn(
            "I'll inspect.",
            "".join(
                event.get("text", "") for event in suppressed_stream
            ),
        )
        self.assertIn(
            "tool_call", [event["type"] for event in suppressed_stream]
        )
        self.assertEqual(included.text, "I'll inspect. Done.")
        self.assertEqual(
            "".join(
                event.get("text", "") for event in included_stream
            ),
            "I'll inspect. Done.",
        )
        self.assertIn("tool_call", [event["type"] for event in included_stream])

    async def test_advanced_bridge_controls_input_and_visible_output(self):
        target = _Target()
        bridge = Agent.advanced(
            target=target,
            input_adapter=lambda text, state: {
                "counter": state["counter"],
                "prompt": text.upper(),
            },
            output_adapter=lambda result: f"bridge:{result['counter']}",
        )
        driver = LangGraphRuntimeDriver(_application(target, bridge=bridge))
        await driver.create_session(
            session_id="session-1", user_id="user-1", state={"counter": 8}
        )

        result = await driver.invoke(_request())

        self.assertEqual(target.inputs[0]["prompt"], "HELLO")
        self.assertEqual(result.text, "bridge:9")
        self.assertEqual(result.result, "bridge:9")
        await driver.close()

    async def test_driver_materializes_and_closes_one_mcp_client(self):
        configured = replace(
            MCPClient.sse(
                "https://mcp.example/sse",
                prefix="legacy",
                tools=("echo",),
            ),
            capability_id="mcp__legacy",
            approval=ApprovalPolicy(
                message="Approve echo?",
                tools=("echo",),
            ),
        )
        definition = AgentDefinition(
            name="mcp_agent",
            model="openai:test",
            instruction="Use MCP tools.",
            mcp=(configured,),
        )
        plan = ManagedAgentPlan(definition, tools=("local-tool",))
        target = _Target()
        driver = LangGraphRuntimeDriver(_application(plan))
        await driver.create_session(
            session_id="session-1", user_id="user-1", state={"counter": 0}
        )
        adapters = ModuleType("langchain_mcp_adapters")
        adapters.__path__ = []
        client_module = ModuleType("langchain_mcp_adapters.client")
        client_module.MultiServerMCPClient = _MCPAdapterClient

        with patch.dict(
            sys.modules,
            {
                "langchain_mcp_adapters": adapters,
                "langchain_mcp_adapters.client": client_module,
            },
        ), patch(
            "harnest.backends.langgraph.materialize_agent",
            return_value=target,
        ) as materialize:
            first = await driver.invoke(_request())
            second = await driver.invoke(_request())

        self.assertEqual(first.text, "answer-1")
        self.assertEqual(second.text, "answer-2")
        self.assertEqual(len(_MCPAdapterClient.instances), 1)
        client = _MCPAdapterClient.instances[0]
        self.assertEqual(client.requests, ["mcp__legacy"])
        self.assertEqual(client.connections["mcp__legacy"]["transport"], "sse")
        self.assertTrue(client.tool_name_prefix)
        self.assertEqual(len(client.tool_interceptors), 1)
        passed_plan, passed_tools = materialize.call_args.args
        self.assertIs(passed_plan, plan)
        self.assertEqual([tool.name for tool in passed_tools], ["mcp__legacy_echo"])

        await driver.close()

        self.assertEqual(target.closed, 1)
        self.assertEqual(client.closed, 1)

    async def test_mcp_gateway_lifecycle_wraps_adapter_owned_sessions(self):
        lifecycle = _RuntimeMCPLifecycle()
        configured = replace(
            MCPClient.streamable_http(
                "https://gateway.example/mcp",
                lifecycle=lifecycle,
            ),
            capability_id="mcp__gateway",
        )
        plan = ManagedAgentPlan(
            AgentDefinition(
                name="gateway_agent",
                model="openai:test",
                instruction="Use the gateway.",
                mcp=(configured,),
            ),
            tools=(),
        )
        driver = LangGraphRuntimeDriver(_application(plan))
        await driver.create_session(
            session_id="session-1", user_id="user-1", state={}
        )
        adapters = ModuleType("langchain_mcp_adapters")
        adapters.__path__ = []
        client_module = ModuleType("langchain_mcp_adapters.client")
        client_module.MultiServerMCPClient = _MCPAdapterClient

        with patch.dict(
            sys.modules,
            {
                "langchain_mcp_adapters": adapters,
                "langchain_mcp_adapters.client": client_module,
            },
        ), patch(
            "harnest.backends.langgraph.materialize_agent",
            return_value=_Target(),
        ):
            await driver.invoke(_request())

        self.assertEqual(
            lifecycle.events,
            ["start:langgraph", "create:langgraph"],
        )
        await driver.close()
        self.assertEqual(
            lifecycle.events,
            [
                "start:langgraph",
                "create:langgraph",
                "session-close",
                "lifecycle-close:langgraph",
            ],
        )

    async def test_driver_materializes_mcp_agents_inside_graph(self):
        definition = AgentDefinition(
            name="graph_agent",
            model="openai:test",
            instruction="Use MCP tools.",
            mcp=(
                MCPClient.sse(
                    "https://mcp.example/sse", prefix="graph", tools=("echo",)
                ),
            ),
        )
        graph = Graph(
            name="mcp_graph",
            nodes={"primary": definition, "secondary": definition},
            edges=(Edge(START, "primary"), Edge(START, "secondary")),
        )
        plan = ManagedGraphPlan(graph)
        target = _Target()
        driver = LangGraphRuntimeDriver(_application(plan))
        await driver.create_session(
            session_id="session-1", user_id="user-1", state={"counter": 0}
        )
        adapters = ModuleType("langchain_mcp_adapters")
        adapters.__path__ = []
        client_module = ModuleType("langchain_mcp_adapters.client")
        client_module.MultiServerMCPClient = _MCPAdapterClient

        with patch.dict(
            sys.modules,
            {
                "langchain_mcp_adapters": adapters,
                "langchain_mcp_adapters.client": client_module,
            },
        ), patch(
            "harnest.backends.langgraph.materialize_graph",
            return_value=target,
        ) as materialize:
            result = await driver.invoke(_request())

        self.assertEqual(result.text, "answer-1")
        self.assertEqual(len(_MCPAdapterClient.instances), 1)
        client = _MCPAdapterClient.instances[-1]
        self.assertEqual(client.requests, ["graph"])
        self.assertEqual(client.tool_interceptors, [])
        passed_plan, tool_groups = materialize.call_args.args
        self.assertIs(passed_plan, plan)
        self.assertEqual([tool.name for tool in tool_groups[0]], ["graph_echo"])
        self.assertIs(tool_groups[0], tool_groups[1])

        await driver.close()
        self.assertEqual(target.closed, 1)
        self.assertEqual(client.closed, 1)

    async def test_close_is_idempotent_and_rejects_new_work(self):
        await self.driver.close()
        await self.driver.close()

        self.assertEqual(self.target.closed, 1)
        with self.assertRaises(RuntimeError):
            await self.driver.list_sessions(user_id="user-1")

    async def test_injected_session_store_is_authoritative_and_not_owned(self):
        store = _RecordingSessionStore()
        target = _Target()
        driver = LangGraphRuntimeDriver(
            _application(target),
            session_store=store,
        )
        await driver.create_session(
            session_id="session-1", user_id="user-1", state={"counter": 4}
        )

        result = await driver.invoke(_request())
        stored = await store.get(session_id="session-1", user_id="user-1")
        await driver.close()

        self.assertEqual(result.text, "answer-5")
        self.assertEqual(stored.state["counter"], 5)
        self.assertFalse(store.closed)


if __name__ == "__main__":
    unittest.main()
