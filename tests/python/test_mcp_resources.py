"""Real MCP subprocess coverage for discovery, retrieval, policy, and subscriptions."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from harnest.mcp import MCPClient, MCPResourceError, MCPSubscription
from harnest.mcp_capability_tools import adk_capability_tools, langgraph_capability_tools
from harnest.mcp_lifecycle import close_mcp_lifecycles, start_mcp_lifecycles

SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_capabilities_server.py"


def client(**kwargs):
    return MCPClient.stdio(sys.executable, str(SERVER), **kwargs)


@asynccontextmanager
async def projected_client(configured, framework):
    """Exercise real framework discovery and retain its separate developer registry."""

    if framework == "adk":
        from harnest.mcp_adk import _discover_adk_mcp_clients
        from test_mcp_adk import _native_invocation

        toolset = configured.to_adk_toolset()
        try:
            tools = await toolset.get_tools_with_prefix()
            target = SimpleNamespace(tools=[toolset], sub_agents=[])
            clients = await _discover_adk_mcp_clients(target, _native_invocation())
            yield tools, clients
        finally:
            await toolset.close()
        return
    from harnest.agent import AgentDefinition
    from harnest.application import CompiledApplication
    from harnest.backends.langgraph import ManagedAgentPlan
    from harnest.runtime_langgraph import LangGraphRuntimeDriver

    definition = AgentDefinition(name="projection", model="unused", instruction="Inspect capabilities.")
    app = CompiledApplication(name="projection", framework="langgraph", mode="managed", target=ManagedAgentPlan(definition))
    driver = LangGraphRuntimeDriver(app)
    try:
        tools = await driver._resolve_tool_group((configured,))
        yield tools, driver._mcp_context_clients
    finally:
        await driver.close()


class MCPResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_frameworks_expose_only_advertised_model_helpers(self):
        """Cover tools-only, resource-only, prompt-only, and mixed real MCP servers."""

        resources = {"harnest_list_resources", "harnest_list_resource_templates", "harnest_read_resource"}
        prompts = {"harnest_list_prompts", "harnest_get_prompt"}
        cases = (("--tools-only", set(), 1), ("--resources-only", resources, 0),
                 ("--prompts-only", prompts, 0), ("--mixed", resources | prompts, 1))
        for framework in ("adk", "langgraph"):
            for flag, expected, remote_count in cases:
                with self.subTest(framework=framework, server=flag):
                    configured = replace(MCPClient.stdio(sys.executable, str(SERVER), flag), identity="knowledge", capability_id="knowledge")
                    async with projected_client(configured, framework) as (tools, clients):
                        helpers = {getattr(tool, "__harnest_mcp_capability__", None) for tool in tools}
                        self.assertEqual(helpers - {None}, expected)
                        self.assertEqual(len(tools), len(expected) + remote_count)
                        self.assertIn("harnest_inspect", clients["knowledge"])
                        self.assertIn("harnest_list_tools", clients["knowledge"])

    def test_empty_allowlists_hide_only_the_denied_model_family(self):
        """Use advertisements, never catalogue contents, to select retrieval helpers."""

        from mcp.types import ServerCapabilities, ResourcesCapability, PromptsCapability
        from harnest.mcp_capability_tools import model_capability_tools

        capabilities = ServerCapabilities(resources=ResourcesCapability(), prompts=PromptsCapability())
        for resources, prompts, expected in ((None, None, 5), ([], None, 2), (None, [], 3), ([], [], 0)):
            configured = client(resources=resources, prompts=prompts)
            helpers = adk_capability_tools(configured, [])
            self.assertEqual(len(model_capability_tools(helpers, configured, capabilities)), expected)

    async def test_resource_only_portable_server_retains_capabilities(self):
        """A plugin's persistent stdio owner must not require remote tools to expose resources."""

        portable = SimpleNamespace(
            prepare=lambda: None,
            stdio=lambda configured: {"command": configured.command, "args": list(configured.args)},
            failed=Mock(),
        )
        configured = replace(MCPClient.stdio(sys.executable, str(SERVER), "--resources-only"),
                             identity="knowledge", capability_id="knowledge", portable=portable)
        async with projected_client(configured, "langgraph") as (tools, _):
            self.assertEqual({tool.__harnest_mcp_capability__ for tool in tools},
                             {"harnest_list_resources", "harnest_list_resource_templates", "harnest_read_resource"})
        portable.failed.assert_not_called()

    async def test_tools_only_developer_inspection_remains_governed_and_revocable(self):
        """Hidden model helpers retain the same scoped developer dispatch and permissions."""

        from harnest import context
        from harnest.context import activate_context, revoke_context
        from harnest.mcp_context import _activate_mcp_context, MCPContextUnavailableError
        from harnest.agent_principal import activate_agent_principal, create_agent_principal_binding, revoke_agent_principal
        from harnest.agent import AgentRuntimePermissionError, AgentRuntimePrincipal
        from test_mcp_adk import _agent_context

        for framework in ("adk", "langgraph"):
            configured = replace(MCPClient.stdio(sys.executable, str(SERVER), "--tools-only", permission="knowledge.connect"), identity="knowledge", capability_id="knowledge")
            async with projected_client(configured, framework) as (_, clients):
                active = _agent_context()
                allowed = create_agent_principal_binding(AgentRuntimePrincipal.create(permissions={"knowledge.connect"}))
                denied = create_agent_principal_binding(AgentRuntimePrincipal.create())
                try:
                    with activate_context(active), _activate_mcp_context(clients):
                        facade = context.mcp("knowledge")
                        with activate_agent_principal(allowed):
                            self.assertEqual((await facade.inspect())["tools"]["tools"][0]["name"], "echo")
                            self.assertEqual(await facade.list_resources(), {"resources": []})
                        with patch("harnest.mcp_resources.resource_session") as transport:
                            with activate_agent_principal(denied), self.assertRaises(AgentRuntimePermissionError):
                                await facade.inspect()
                            transport.assert_not_called()
                    with self.assertRaises(MCPContextUnavailableError):
                        await facade.inspect()
                finally:
                    revoke_context(active)
                    revoke_agent_principal(allowed)
                    revoke_agent_principal(denied)

    async def test_shutdown_cancellation_wins_over_simultaneous_notification(self):
        """Force Python 3.10's completed-wait cancellation race without timing sleeps."""

        from harnest.mcp_subscriptions import _ResourceListener

        handler = AsyncMock()
        worker = _ResourceListener(client(), MCPSubscription("knowledge://handbook", handler), "langgraph")
        session = SimpleNamespace(subscribe_resource=AsyncMock())
        initialized = SimpleNamespace(capabilities=SimpleNamespace(resources=SimpleNamespace(subscribe=True)))
        closed = asyncio.Event()

        @asynccontextmanager
        async def connection(*args, **kwargs):
            try:
                yield session, initialized
            finally:
                closed.set()

        async def notification_and_shutdown():
            # Match close() exactly as a notification wait completes. wait_for
            # on Python 3.10 used to discard this external cancellation.
            worker.stopping = True
            worker.task.cancel()
            return True

        worker.dirty.wait = notification_and_shutdown
        with patch("harnest.mcp_subscriptions.resource_session", connection), patch.object(worker, "connected", AsyncMock()), patch.object(worker, "deliver", AsyncMock()) as deliver:
            worker.task = asyncio.create_task(worker._run())
            try:
                done, _ = await asyncio.wait({worker.task}, timeout=1)
                self.assertIn(worker.task, done, "listener ignored shutdown cancellation")
                self.assertTrue(closed.is_set())
                deliver.assert_not_awaited()
            finally:
                worker.dirty.wait = AsyncMock(side_effect=asyncio.CancelledError)
                await worker.close()

    async def test_handler_completion_does_not_swallow_listener_cancellation(self):
        """A completed handler must not turn shutdown cancellation into a committed delivery."""

        from harnest.mcp_subscriptions import _ResourceListener

        async def handler(event):
            worker.task.cancel()

        worker = _ResourceListener(client(), MCPSubscription("knowledge://handbook", handler), "langgraph")
        read = AsyncMock(return_value={"contents": [{"text": "retained"}]})
        worker.task = asyncio.create_task(worker.deliver(read, "update"))
        with self.assertRaises(asyncio.CancelledError):
            await worker.task
        self.assertIsNone(worker.digest)

    async def test_handler_deadline_cancels_work_without_committing_delivery(self):
        """The cancellation-safe deadline must still stop a stalled authored handler."""

        from harnest.mcp_subscriptions import _ResourceListener

        stopped = asyncio.Event()

        async def handler(event):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        subscription = MCPSubscription("knowledge://handbook", handler, handler_timeout_seconds=0.01)
        worker = _ResourceListener(client(), subscription, "langgraph")
        read = AsyncMock(return_value={"contents": [{"text": "retained"}]})
        with self.assertRaises(TimeoutError):
            await worker.deliver(read, "update")
        self.assertTrue(stopped.is_set())
        self.assertIsNone(worker.digest)

    async def test_serving_pipeline_starts_subscriptions_before_any_invocation(self):
        from google.adk.apps import App
        from harnest.agent import AgentDefinition
        from harnest.application import CompiledApplication
        from harnest.backends.langgraph import ManagedAgentPlan
        from harnest.runtime_adk import ADKRuntimeDriver
        from harnest.runtime_langgraph import LangGraphRuntimeDriver
        from harnest.runtime_pipeline import build_runtime_pipeline

        for framework in ("adk", "langgraph"):
            received = []

            async def handler(event):
                received.append(event)

            definition = AgentDefinition(name="listener", model="unused", instruction="Listen without invoking the model.", mcp=(client(subscriptions=[MCPSubscription("knowledge://handbook", handler)]),))
            target = definition.build() if framework == "adk" else ManagedAgentPlan(definition)
            native_app = App(name="listener", root_agent=target) if framework == "adk" else None
            application = CompiledApplication(name="listener", framework=framework, mode="managed", target=target, native_app=native_app)
            backend = ADKRuntimeDriver(application) if framework == "adk" else LangGraphRuntimeDriver(application)
            driver = build_runtime_pipeline(backend, application.runtime_capabilities, ())
            try:
                await driver.start()
                self.assertEqual(received[0].reason, "start", framework)
            finally:
                await asyncio.wait_for(driver.close(), 5)

    async def test_developer_connection_cannot_be_used_after_its_scope(self):
        async with client().connect() as connection:
            pass
        with patch("harnest.mcp_resources.resource_session") as session:
            with self.assertRaisesRegex(MCPResourceError, "closed"):
                await connection.inspect()
            session.assert_not_called()

    async def test_filtered_empty_page_keeps_cursor_and_unsupported_lists_are_empty(self):
        from mcp import types

        session = SimpleNamespace(list_resources=AsyncMock(return_value=types.ListResourcesResult(
            resources=[types.Resource(name="private", uri="knowledge://private")], nextCursor="opaque/next=2",
        )))
        capabilities = types.ServerCapabilities(resources=types.ResourcesCapability())

        @asynccontextmanager
        async def connection(*args):
            yield session, SimpleNamespace(capabilities=capabilities)

        with patch("harnest.mcp_resources.resource_session", connection):
            async with client(resources=()).connect() as resource_client:
                page = await resource_client.list_resources("opaque/first=1")
                self.assertEqual(page, {"resources": [], "nextCursor": "opaque/next=2"})
                self.assertEqual(await resource_client.list_prompts(), {"prompts": []})
        session.list_resources.assert_awaited_once_with(cursor="opaque/first=1")

    async def test_real_server_discovers_resources_templates_and_prompt_arguments(self):
        async with client().connect() as connection:
            catalog = await connection.inspect()
            self.assertEqual(catalog["tools"]["tools"][0]["name"], "echo")
            self.assertEqual(catalog["resources"]["resources"][0]["uri"], "knowledge://handbook")
            self.assertEqual(catalog["resource_templates"]["resourceTemplates"][0]["uriTemplate"], "knowledge://documents/{id}")
            self.assertEqual(catalog["prompts"]["prompts"][0]["arguments"][0]["name"], "topic")
            resource = await connection.read_resource("knowledge://handbook")
            self.assertEqual(resource["contents"][0]["text"], "# Handbook v0")
            prompt = await connection.get_prompt("summarize", {"topic": "MCP"})
            self.assertEqual(prompt["messages"][0]["role"], "user")
            self.assertEqual(prompt["messages"][0]["content"]["text"], "Summarize MCP")

    async def test_filters_and_bounds_apply_before_exposing_content(self):
        async with client(resources=(), prompts=()).connect() as connection:
            self.assertEqual((await connection.list_resources())["resources"], [])
            self.assertEqual((await connection.list_prompts())["prompts"], [])
            with patch("harnest.mcp_resources.resource_session") as session:
                with self.assertRaises(MCPResourceError):
                    await connection.read_resource("file:///etc/passwd")
                with self.assertRaises(MCPResourceError):
                    await connection.get_prompt("summarize")
                session.assert_not_called()
        async with client(max_content_bytes=1024).connect() as connection:
            with self.assertRaisesRegex(MCPResourceError, "max_content_bytes"):
                await connection.read_resource("knowledge://large")

    async def test_both_frameworks_render_native_tool_schemas_and_retrieve_prompt(self):
        configured = client()
        adk = adk_capability_tools(configured, [])
        for tool in adk:
            self.assertIsNotNone(tool._get_declaration())
            self.assertEqual(tool._get_declaration().name, tool.name)
        result = await adk[-1].run_async(args={"name": "summarize", "arguments": {"topic": "ADK"}}, tool_context=None)
        self.assertEqual(result["messages"][0]["content"]["text"], "Summarize ADK")
        graph = langgraph_capability_tools(configured, "knowledge", [])
        result = await graph[-1].ainvoke({"name": "summarize", "arguments": {"topic": "LangGraph"}})
        self.assertEqual(result["messages"][0]["content"]["text"], "Summarize LangGraph")

    async def test_runtime_subscription_receives_start_and_update_then_closes(self):
        events = []
        updated = asyncio.Event()

        async def handler(event):
            events.append(event)
            if event.reason == "update":
                updated.set()

        configured = client(subscriptions=[MCPSubscription("knowledge://handbook", handler)])
        bindings = configured._runtime_bindings("langgraph")
        try:
            await start_mcp_lifecycles(bindings)
            await asyncio.wait_for(updated.wait(), 5)
        finally:
            await close_mcp_lifecycles(bindings)
        self.assertEqual([event.reason for event in events], ["start", "update"])
        self.assertEqual(events[-1].resource["contents"][0]["text"], "# Handbook v1")

    async def test_cli_inspection_does_not_start_subscription_handlers(self):
        async def forbidden(event):
            raise AssertionError("inspection must not start listeners")

        configured = client(subscriptions=[MCPSubscription("knowledge://handbook", forbidden)])
        async with configured.connect() as connection:
            self.assertIn("capabilities", await connection.inspect())
        self.assertEqual(configured._subscription_controller._state, "new")

    async def test_resource_only_server_still_exposes_agent_context_tools(self):
        configured = MCPClient.stdio(sys.executable, str(SERVER), "--resources-only")
        toolset = configured.to_adk_toolset()
        try:
            tools = await toolset.get_tools()
            self.assertEqual(len(tools), 3)
            self.assertTrue(all(getattr(tool, "__harnest_mcp_capability__", None) for tool in tools))
        finally:
            await toolset.close()

    async def test_adk_capability_names_are_unique_across_unprefixed_clients(self):
        first = adk_capability_tools(replace(client(), identity="first"), [])
        second = adk_capability_tools(replace(client(), identity="second"), [])
        self.assertFalse({tool.name for tool in first} & {tool.name for tool in second})
        self.assertEqual(first[0].__harnest_mcp_capability__, "harnest_inspect")

    def test_subscription_validates_transport_and_allowlist(self):
        async def handler(event):
            pass

        subscription = MCPSubscription("knowledge://handbook", handler)
        with self.assertRaises(ValueError):
            client(resources=(), subscriptions=[subscription])
        with self.assertRaises(ValueError):
            client(subscriptions=[replace(subscription, method="subscriptions/listen")])
        with self.assertRaises(TypeError):
            MCPSubscription("knowledge://handbook", lambda event: None)


if __name__ == "__main__":
    unittest.main()
