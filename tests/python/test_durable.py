import inspect
import unittest
from types import SimpleNamespace

from harnest.durable import (
    NativeDurableSuspended,
    NativeResumeInput,
    ResumeArtifact,
    adk_durable_tool,
    current_native_durable_call,
    langgraph_durable_callable,
)
from harnest.tool import tool


class DurableToolTests(unittest.IsolatedAsyncioTestCase):
    def test_durable_marker_requires_an_async_tool(self):
        with self.assertRaisesRegex(TypeError, "durable tools must be async"):

            @tool(durable=True)
            def invalid(value: str) -> str:
                """Return one value."""

                return value

    async def test_adk_wrapper_binds_native_call_identity(self):
        @tool(durable=True)
        async def inspect_call(value: str) -> dict[str, str]:
            """Return the native identity visible inside a durable call."""

            active = current_native_durable_call()
            self.assertIsNotNone(active)
            assert active is not None
            return {
                "value": value,
                "invocation": active.artifact.native_invocation_id,
                "call": active.artifact.tool_call_id,
            }

        native = adk_durable_tool(inspect_call)
        self.assertTrue(native.is_long_running)
        context = SimpleNamespace(
            invocation_id="native-invocation",
            function_call_id="function-call",
            tool_confirmation=None,
        )
        result = await native.run_async(args={"value": "ok"}, tool_context=context)

        self.assertEqual(
            result,
            {
                "value": "ok",
                "invocation": "native-invocation",
                "call": "function-call",
            },
        )
        self.assertIsNone(current_native_durable_call())

    async def test_langgraph_injected_fields_are_hidden_from_model_schema(self):
        from langchain_core.tools import tool as langchain_tool

        observed = []

        @tool(durable=True)
        async def inspect_call(value: str) -> dict[str, str]:
            """Return the native identity visible inside a durable call."""

            active = current_native_durable_call()
            self.assertIsNotNone(active)
            assert active is not None
            observed.append(active.artifact)
            return {
                "value": value,
                "thread": active.artifact.native_invocation_id,
                "call": active.artifact.tool_call_id,
            }

        wrapped = langgraph_durable_callable(inspect_call)
        native = langchain_tool(wrapped)
        schema = native.tool_call_schema.model_json_schema()
        self.assertEqual(set(schema["properties"]), {"value"})
        result = await native.ainvoke(
            {
                "type": "tool_call",
                "id": "tool-call",
                "name": "inspect_call",
                "args": {"value": "ok"},
            },
            config={"configurable": {"thread_id": "tenant/session/thread"}},
        )

        self.assertEqual(result.tool_call_id, "tool-call")
        self.assertEqual(observed[0].native_invocation_id, "tenant/session/thread")
        self.assertEqual(observed[0].tool_call_id, "tool-call")
        self.assertIsNone(current_native_durable_call())

    async def test_langgraph_checkpoint_reconstructs_native_interrupt(self):
        """Prove durable tools pause and resume through a real persisted graph."""

        from langchain_core.messages import AIMessage
        from langchain_core.tools import tool as langchain_tool
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.graph import END, START, MessagesState, StateGraph
        from langgraph.prebuilt import ToolNode
        from langgraph.types import Command

        observed = []

        @tool(durable=True)
        async def pause(value: str) -> dict[str, bool]:
            """Pause at a framework-owned continuation boundary."""

            del value
            active = current_native_durable_call()
            assert active is not None
            observed.append(active.artifact)
            from harnest.external_continuation import PendingExternalContinuation

            return active.suspend(
                PendingExternalContinuation("pending-1", "durable.demo")
            )

        builder = StateGraph(MessagesState)
        builder.add_node(
            "tools", ToolNode([langchain_tool(langgraph_durable_callable(pause))])
        )
        builder.add_edge(START, "tools")
        builder.add_edge("tools", END)
        graph = builder.compile(checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "tenant/session/thread"}}
        first = await graph.ainvoke(
            {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "pause",
                                "args": {"value": "private"},
                                "id": "call-1",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            },
            config,
        )
        second = await graph.ainvoke(Command(resume={"done": True}), config)

        self.assertEqual(first["__interrupt__"][0].value["id"], "pending-1")
        self.assertEqual(second["messages"][-1].content, '{"done": true}')
        self.assertEqual(len(observed), 2)
        self.assertEqual(observed[0], observed[1])

    async def test_langgraph_driver_resumes_on_another_compiled_instance(self):
        """Resume one ToolNode through a separate graph over the shared saver."""

        from langchain_core.messages import AIMessage
        from langchain_core.tools import tool as langchain_tool
        from langgraph.graph import END, START, MessagesState, StateGraph
        from langgraph.prebuilt import ToolNode

        from harnest.application import CompiledApplication
        from harnest.checkpoint import MemoryStore
        from harnest.checkpoint_langgraph import HarnestCheckpointSaver
        from harnest.external_continuation import PendingExternalContinuation
        from harnest.runtime_contract import InvocationRequest
        from harnest.runtime_langgraph import LangGraphRuntimeDriver

        observed = []

        @tool(durable=True)
        async def pause(value: str) -> dict[str, bool]:
            """Suspend once and return the value supplied by native replay."""

            del value
            active = current_native_durable_call()
            assert active is not None
            observed.append(active.artifact)
            return active.suspend(
                PendingExternalContinuation("pending-2", "durable.driver")
            )

        native_tool = langchain_tool(langgraph_durable_callable(pause))

        def compiled_graph(store: MemoryStore):
            """Build the equivalent graph owned independently by each replica."""

            async def request_tool(_state):
                return {
                    "messages": [
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "pause",
                                    "args": {"value": "private"},
                                    "id": "call-2",
                                    "type": "tool_call",
                                }
                            ],
                        )
                    ]
                }

            async def reply(state):
                return {"messages": [AIMessage(content=state["messages"][-1].content)]}

            builder = StateGraph(MessagesState)
            builder.add_node("request", request_tool)
            builder.add_node("tools", ToolNode([native_tool]))
            builder.add_node("reply", reply)
            builder.add_edge(START, "request")
            builder.add_edge("request", "tools")
            builder.add_edge("tools", "reply")
            builder.add_edge("reply", END)
            return builder.compile(checkpointer=HarnestCheckpointSaver(store))

        def application(store: MemoryStore) -> CompiledApplication:
            """Create one replica with a distinct graph and saver instance."""

            return CompiledApplication(
                name="durable-driver",
                framework="langgraph",
                mode="advanced",
                target=compiled_graph(store),
                kind="advanced",
                checkpointer=store,
                session_store=store,
            )

        def request(value, *, state_delta=None) -> InvocationRequest:
            """Keep the run identity stable while swapping only native input."""

            return InvocationRequest(
                input=value,
                user_id="user-1",
                session_id="session-1",
                invocation_id="run-1",
                metadata={"source": "durable-test"},
                state_delta=state_delta or {},
            )

        store = MemoryStore()
        await store.start()
        first = LangGraphRuntimeDriver(application(store), session_store=store)
        second = LangGraphRuntimeDriver(application(store), session_store=store)
        try:
            await first.create_session(
                session_id="session-1", user_id="user-1", state={}
            )
            with self.assertRaises(NativeDurableSuspended):
                await first.invoke(request("start"))

            artifact = observed[0]
            resumed = await second.invoke(
                request(
                    NativeResumeInput(artifact, {"done": True}),
                    state_delta={"must_not_apply": True},
                )
            )
            session = await second.get_session(
                session_id="session-1", user_id="user-1"
            )

            self.assertEqual(resumed.text, '{"done": true}')
            self.assertEqual(len(observed), 2)
            self.assertEqual(observed[0], observed[1])
            self.assertNotIn("must_not_apply", session.state)
        finally:
            await first.close()
            await second.close()
            await store.close()

    async def test_langgraph_driver_rejects_mismatched_resume_thread(self):
        """Do not let a persisted artifact select another checkpoint thread."""

        from harnest.application import CompiledApplication
        from harnest.runtime_contract import InvocationRequest
        from harnest.runtime_langgraph import LangGraphRuntimeDriver

        class Target:
            def __init__(self) -> None:
                self.called = False

            async def ainvoke(self, value, *, config, durability=None):
                self.called = True
                return value, config, durability

        target = Target()
        driver = LangGraphRuntimeDriver(
            CompiledApplication(
                name="durable-driver",
                framework="langgraph",
                mode="advanced",
                target=target,
                kind="advanced",
            )
        )
        await driver.create_session(
            session_id="session-1", user_id="user-1", state={}
        )
        artifact = ResumeArtifact(
            "langgraph", "another-thread", "call-1", "pause"
        )
        request = InvocationRequest(
            input=NativeResumeInput(artifact, {"done": True}),
            user_id="user-1",
            session_id="session-1",
            invocation_id="run-1",
            metadata={},
            state_delta={},
        )
        try:
            with self.assertRaisesRegex(ValueError, "does not match request"):
                await driver.invoke(request)
            self.assertFalse(target.called)
        finally:
            await driver.close()

    def test_resume_artifact_rejects_unbounded_identity(self):
        with self.assertRaisesRegex(ValueError, "tool_call_id"):
            ResumeArtifact("adk", "invocation", "contains spaces", "tool")

    def test_langgraph_wrapper_retains_authored_signature_fields(self):
        @tool(durable=True)
        async def lookup(query: str, *, limit: int = 5) -> str:
            """Look up one query."""

            return query[:limit]

        parameters = inspect.signature(langgraph_durable_callable(lookup)).parameters
        self.assertEqual(tuple(parameters)[:2], ("query", "limit"))


if __name__ == "__main__":
    unittest.main()
