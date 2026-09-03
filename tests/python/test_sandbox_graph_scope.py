"""Unassigned graph nodes cannot borrow a same-named managed agent's grants."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from google.adk.workflow import BaseNode
from langgraph.graph import END, START as LG_START, StateGraph

from harnest import Agent, Sandbox
from harnest.context import (
    ContextUnavailableError, activate_context, context, create_agent_context,
)
from harnest.context_sandboxes import SandboxRegistry
from harnest.graph import START, Edge, Event, Graph


class SandboxGraphScopeTests(unittest.IsolatedAsyncioTestCase):
    """Exercise backend lowering, native scheduling, and iterator boundaries."""

    def setUp(self):
        """Give the caller a real grant whose factory must remain untouched."""
        self.factory = Mock(side_effect=AssertionError("unassigned node started provider"))
        self.sandbox = Sandbox.provider(self.factory)
        self.observed = []

    def _scope(self, framework):
        """Reproduce a graph identity colliding with an authorized agent name."""
        registry = SandboxRegistry({"shared_identity": {"calculations": self.sandbox}}, framework=framework)
        return create_agent_context(
            framework=framework, agent_name="shared_identity", invocation_id="call",
            user_id="user", session_id="session", metadata={}, resources={},
            sandbox_registry=registry,
        )

    def _denied(self, label):
        """Check both fresh lookup and a caller-retained capability before startup."""
        with self.assertRaises(ContextUnavailableError):
            context.sandboxes["calculations"].execute("print(42)")
        with self.assertRaises(ContextUnavailableError):
            self.retained.execute("print(42)")
        self.observed.append(label)

    def _caller_restored(self, active):
        """A yielded event must not leave denial active in its authorized consumer."""
        self.assertIs(context.current(), active)
        self.assertIsNotNone(context.sandboxes["calculations"])
        self.factory.assert_not_called()

    def _graph(self, node):
        """Lower a raw component beneath the colliding public graph identity."""
        return Graph(name="shared_identity", nodes={"raw": node}, edges=(Edge(START, "raw"),))

    async def _consume_adk(self, node, *, close_early=False):
        """Execute the actual node produced by the ADK graph backend hook."""
        workflow = self._graph(node).build("adk")
        native = next(value for value in workflow.graph.nodes if value.name == "raw")
        active = self._scope("adk")
        native_context = SimpleNamespace(
            state=SimpleNamespace(to_dict=lambda: {}), actions=SimpleNamespace(state_delta={}),
        )
        with activate_context(active):
            self.retained = context.sandboxes["calculations"]
            events = native.run(ctx=native_context, node_input="input")
            try:
                first = await anext(events)
                self.assertIsNotNone(first)
                self._caller_restored(active)
                if not close_early:
                    async for _ in events:
                        self._caller_restored(active)
            finally:
                await events.aclose()
            self._caller_restored(active)

    async def test_adk_sync_callable_denies_same_named_root_grant(self):
        """A lowered synchronous function is not a managed sandbox consumer."""
        def raw(value):
            """Attempt both unauthorized capability access paths."""
            self._denied("sync")
            return Event(output=value)

        await self._consume_adk(raw)
        self.assertEqual(self.observed, ["sync"])

    async def test_adk_async_callable_denies_after_await(self):
        """The denial boundary survives asynchronous callable scheduling."""
        async def raw(value):
            """Attempt access after yielding control to the event loop."""
            await asyncio.sleep(0)
            self._denied("async")
            return Event(output=value)

        await self._consume_adk(raw)
        self.assertEqual(self.observed, ["async"])

    async def test_adk_native_node_stream_and_early_close_stay_denied(self):
        """Native BaseNode iteration and finalization never inherit root grants."""
        owner = self

        class RawNode(BaseNode):
            """Observe native generator advancement and early finalization."""

            async def _run_impl(self, *, ctx, node_input):
                """Probe authorization before each yield and during cleanup."""
                try:
                    owner._denied("first")
                    yield node_input
                    owner._denied("second")
                    yield node_input
                finally:
                    owner._denied("closed")

        await self._consume_adk(RawNode(name="raw"), close_early=True)
        self.assertEqual(self.observed, ["first", "closed"])
        self.observed.clear()
        await self._consume_adk(RawNode(name="raw"))
        self.assertEqual(self.observed, ["first", "second", "closed"])

    async def test_langgraph_sync_callable_denies_and_restores_caller(self):
        """Run a raw callable through the complete lowered native graph."""
        def raw(value):
            """Attempt access inside LangGraph's synchronous worker."""
            self._denied("sync")
            return Event(output=value)

        compiled = self._graph(raw).build("langgraph")
        active = self._scope("langgraph")
        with activate_context(active):
            self.retained = context.sandboxes["calculations"]
            result = compiled.invoke({"value": "input"})
            self.assertEqual(result["value"], "input")
            self._caller_restored(active)
        self.assertEqual(self.observed, ["sync"])

    async def test_langgraph_async_callable_denies_and_restores_caller(self):
        """Run asynchronous raw code through real native task scheduling."""
        async def raw(value):
            """Check denial survives a scheduling boundary."""
            await asyncio.sleep(0)
            self._denied("async")
            return Event(output=value)

        compiled = self._graph(raw).build("langgraph")
        active = self._scope("langgraph")
        with activate_context(active):
            self.retained = context.sandboxes["calculations"]
            result = await compiled.ainvoke({"value": "input"})
            self.assertEqual(result["value"], "input")
            self._caller_restored(active)
        self.assertEqual(self.observed, ["async"])

    def _native_langgraph(self, advanced=False, trace_streams=False):
        """Use an actual Pregel target and activate its backend embedding hooks."""
        def raw(state):
            """Probe inside the native graph rather than a Harnest callable node."""
            self._denied("native")
            return state

        builder = StateGraph(dict)
        builder.add_node("probe", raw)
        builder.add_edge(LG_START, "probe")
        builder.add_edge("probe", END)
        native = builder.compile()
        if trace_streams:
            self._observe_stream_boundaries(native)
        embedded = Agent.advanced(native) if advanced else native
        outer = self._graph(embedded).build("langgraph")
        self.assertTrue(native._harnest_sandbox_graph_scoped)
        return native, outer

    def _observe_stream_boundaries(self, native):
        """Observe real Pregel streams at creation and close, not only node work."""
        original_sync, original_async = native.stream, native.astream

        def stream(*args, **kwargs):
            """Require denial even before a native stream returns its iterator."""
            self._denied("sync-created")
            events = original_sync(*args, **kwargs)

            def consume():
                """Delegate real events and observe early generator cleanup."""
                try:
                    yield from events
                finally:
                    events.close()
                    self._denied("sync-closed")
            return consume()

        def astream(*args, **kwargs):
            """Require the same protection when constructing an async iterator."""
            self._denied("async-created")
            events = original_async(*args, **kwargs)

            async def consume():
                """Delegate native async events and verify finalizer authority."""
                try:
                    async for event in events:
                        yield event
                finally:
                    await events.aclose()
                    self._denied("async-closed")
            return consume()

        object.__setattr__(native, "stream", stream)
        object.__setattr__(native, "astream", astream)

    async def test_native_langgraph_invoke_paths_and_advanced_embedding(self):
        """Both native embedding paths deny grants in sync and async invocation."""
        for advanced in (False, True):
            with self.subTest(advanced=advanced):
                native, outer = self._native_langgraph(advanced)
                active = self._scope("langgraph")
                with activate_context(active):
                    self.retained = context.sandboxes["calculations"]
                    native.invoke({"value": "input"})
                    self._caller_restored(active)
                    await native.ainvoke({"value": "input"})
                    self._caller_restored(active)
                    await outer.ainvoke({"value": "input"})
                    self._caller_restored(active)
        self.assertEqual(self.observed, ["native"] * 6)

    async def test_native_langgraph_streams_restore_scope_between_events_and_close(self):
        """Native sync/async streaming restores consumer authority on every yield."""
        native, _ = self._native_langgraph(trace_streams=True)
        active = self._scope("langgraph")
        with activate_context(active):
            self.retained = context.sandboxes["calculations"]
            events = native.stream({"value": "input"})
            try:
                self.assertIsNotNone(next(events))
                self._caller_restored(active)
            finally:
                events.close()
            self._caller_restored(active)
            async_events = native.astream({"value": "input"})
            try:
                self.assertIsNotNone(await anext(async_events))
                self._caller_restored(active)
            finally:
                await async_events.aclose()
            self._caller_restored(active)
        self.assertEqual(self.observed, [
            "sync-created", "native", "sync-closed",
            "async-created", "native", "async-closed",
        ])


if __name__ == "__main__":
    unittest.main()
