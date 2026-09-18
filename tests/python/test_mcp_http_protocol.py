"""Protocol selection and framework parity across modern and initialized MCP HTTP."""

import asyncio
from contextlib import asynccontextmanager
import json
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
import anyio

from harnest.mcp import MCPClient, MCPClientLifecycle, MCPResourceError
from harnest.mcp_http_session import modern_session
from harnest.mcp_http_tools import modern_tools
from harnest.mcp_http_tool_headers import tool_headers, valid_tool_schema
from harnest.mcp_http_transport import _call, header_value

URI = "events://orders/latest"
VERSION = "2026-07-28"
SCHEMA = {"type": "object", "properties": {"text": {"type": "string", "x-mcp-header": "Text"}}, "required": ["text"]}


class KeepaliveStream(httpx.AsyncByteStream):
    """Keep the read active until the total deadline or caller cancellation closes it."""

    def __init__(self):
        self.started = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        self.started.set()
        while True:
            yield b": keepalive\n\n"
            await asyncio.sleep(.005)

    async def aclose(self):
        self.closed = True


class Gateway(MCPClientLifecycle):
    """Expose a modern-only server that rejects initialize and records every request."""

    def __init__(self):
        self.requests = []
        self.failure = None
        self.input_required = False
        self.transport_failure = False

    def create_http_client(self, options, context):
        return httpx.AsyncClient(headers=options.headers, transport=httpx.MockTransport(self.respond))

    def respond(self, request):
        message = json.loads(request.content)
        self.requests.append((request.headers, message))
        if self.failure:
            status, code = self.failure
            return httpx.Response(status, json={"jsonrpc": "2.0", "id": message["id"], "error": {"code": code, "message": "private credential"}})
        result = self.result(message)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": {"resultType": "complete", **result}})

    def result(self, message):
        """Keep response dispatch explicit to expose accidental compatibility calls."""

        method, params = message["method"], message["params"]
        results = {
            "server/discover": {"supportedVersions": [VERSION], "capabilities": {"tools": {}, "resources": {"subscribe": True}, "prompts": {}}, "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "modern", "version": "1"}}},
            "tools/list": {"tools": [{"name": "echo", "inputSchema": SCHEMA}]},
            "resources/list": {"resources": [{"name": "latest", "uri": URI}], "nextCursor": "next/page"},
            "resources/templates/list": {"resourceTemplates": []},
            "prompts/list": {"prompts": [{"name": "summarize", "arguments": [{"name": "topic", "required": True}]}]},
            "resources/read": {"contents": [{"uri": URI, "text": "retained"}]},
            "prompts/get": {"messages": [{"role": "user", "content": {"type": "text", "text": "Summarize " + params.get("arguments", {}).get("topic", "")}}]},
        }
        if method == "tools/call":
            if self.transport_failure:
                raise httpx.ReadError("private credential")
            if self.input_required:
                return {"resultType": "input_required", "inputRequests": []}
            return {"content": [{"type": "text", "text": params["arguments"]["text"]}]}
        return results[method]


class MCPHTTPProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.gateway = Gateway()
        self.configured = MCPClient.streamable_http("https://modern.invalid/mcp", lifecycle=self.gateway, prefix="modern")

    async def asyncSetUp(self):
        binding = self.configured._lifecycle_binding("langgraph")
        await binding.start()
        self.addAsyncCleanup(binding.close, reset=True)

    async def test_total_deadline_closes_keepalive_stream_without_replay(self):
        """A busy SSE stream cannot extend the RPC deadline on Python 3.10 or newer."""

        stream = KeepaliveStream()
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), timeout=.03) as client:
            with self.assertRaises(TimeoutError):
                await _call(client, "https://modern.invalid", "tools/call", {"name": "echo", "arguments": {}}, 1024)
        self.assertTrue(stream.started.is_set())
        self.assertTrue(stream.closed)
        self.assertEqual(len(requests), 1)

    async def test_caller_cancellation_closes_stream_and_stays_cancelled(self):
        """The timeout scope must not swallow caller cancellation or leak the stream."""

        stream = KeepaliveStream()

        def respond(request):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), timeout=30) as client:
            task = asyncio.create_task(_call(client, "https://modern.invalid", "server/discover", {}, 1024))
            try:
                await asyncio.wait_for(stream.started.wait(), 1)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertTrue(stream.closed)

    async def test_cli_facade_discovers_and_reads_without_initialize(self):
        async with self.configured.connect() as client:
            catalog = await client.inspect()
            self.assertEqual(catalog["protocolVersion"], VERSION)
            self.assertEqual(catalog["serverInfo"]["name"], "modern")
            self.assertEqual(catalog["resources"]["resources"][0]["uri"], URI)
            self.assertEqual((await client.read_resource(URI))["contents"][0]["text"], "retained")
            self.assertEqual((await client.get_prompt("summarize", {"topic": "MCP"}))["messages"][0]["content"]["text"], "Summarize MCP")
            await client.list_resources("next/page")
        self.assertEqual(self.gateway.requests[-1][1]["params"]["cursor"], "next/page")
        for headers, request in self.gateway.requests:
            self.assertEqual(headers["MCP-Protocol-Version"], VERSION)
            self.assertEqual(headers["Mcp-Method"], request["method"])
            self.assertEqual(request["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"], VERSION)
            self.assertNotIn("Mcp-Session-Id", headers)

    async def test_adk_toolset_and_langgraph_tools_use_modern_calls(self):
        await self.configured._lifecycle_binding("langgraph").close(reset=True)
        binding = self.configured._lifecycle_binding("adk")
        await binding.start()
        toolset = self.configured.to_adk_toolset()
        try:
            tools = await toolset.get_tools()
            result = await tools[0].run_async(args={"text": "ADK"}, tool_context=None)
            self.assertEqual(result["content"][0]["text"], "ADK")
            self.assertIsNotNone(tools[0]._get_declaration())
            context_read = next(tool for tool in tools if tool.name == "harnest_read_resource")
            self.assertEqual((await context_read.run_async(args={"uri": URI}, tool_context=None))["contents"][0]["text"], "retained")
        finally:
            await toolset.close()
            await binding.close(reset=True)
        await self.configured._lifecycle_binding("langgraph").start()
        tools = await modern_tools(self.configured, "langgraph", server_name="modern")
        result = await tools[0].ainvoke({"text": "LangGraph"})
        self.assertEqual(result[0]["text"], "LangGraph")
        self.assertEqual(self.gateway.requests[-1][0]["Mcp-Param-Text"], "LangGraph")
        self.assertNotIn("initialize", [message["method"] for _, message in self.gateway.requests])

    async def test_langgraph_interceptor_still_gates_transport(self):
        async def deny(request, handler):
            raise PermissionError("denied")

        tools = await modern_tools(self.configured, "langgraph", server_name="modern", interceptors=[deny])
        with self.assertRaises(PermissionError):
            await tools[0].ainvoke({"text": "blocked"})
        self.assertNotIn("tools/call", [message["method"] for _, message in self.gateway.requests])

    async def test_fallback_only_for_explicit_protocol_incompatibility(self):
        for code in (-32601, -32022):
            self.gateway.failure = (400, code)
            async with modern_session(self.configured, "langgraph") as modern:
                self.assertIsNone(modern)
        for status, code in ((401, -32601), (403, -32601), (500, -32601), (400, -32020)):
            self.gateway.failure = (status, code)
            with self.assertRaises(Exception):
                async with modern_session(self.configured, "langgraph"):
                    self.fail("must not downgrade")

    async def test_resource_fallback_uses_initialized_sdk(self):
        self.gateway.failure = (404, -32601)
        native = SimpleNamespace()

        @asynccontextmanager
        async def sdk(*args, **kwargs):
            yield native, SimpleNamespace()

        from harnest.mcp_resources import resource_session
        with patch("harnest.mcp_resources.sdk_resource_session", sdk):
            async with resource_session(self.configured) as (session, _):
                self.assertIs(session, native)

    async def test_tool_input_required_and_disconnect_never_replay(self):
        for mode in ("input_required", "transport_failure"):
            self.gateway.input_required = self.gateway.transport_failure = False
            tools = await modern_tools(self.configured, "langgraph", server_name="modern")
            setattr(self.gateway, mode, True)
            self.gateway.requests.clear()
            with self.assertRaises(MCPResourceError) as raised:
                await tools[0].ainvoke({"text": "once"})
            self.assertNotIn("private credential", str(raised.exception))
            self.assertEqual([m["method"] for _, m in self.gateway.requests], ["tools/call"])

    async def test_allowlists_apply_to_modern_resources_and_tools(self):
        configured = MCPClient.streamable_http("https://modern.invalid", lifecycle=Gateway(), resources=[], prompts=[], tools=[])
        async with configured.connect() as client:
            result = await client.inspect()
            for kind in ("resources", "prompts", "tools"):
                self.assertEqual(result[kind][kind], [])
            with self.assertRaises(MCPResourceError):
                await client.read_resource(URI)
            self.assertEqual(await modern_tools(configured, "langgraph"), [])

    async def test_real_sdk_http_server_falls_back_and_reads(self):
        from mcp.server.fastmcp import FastMCP

        server = FastMCP("initialized", stateless_http=True, json_response=True)

        @server.resource(URI)
        def retained() -> str:
            return "older retained"

        app = server.streamable_http_app()

        class LegacyGateway(MCPClientLifecycle):
            def create_http_client(self, options, context):
                return httpx.AsyncClient(headers=options.headers, transport=httpx.ASGITransport(app))

        configured = MCPClient.streamable_http("http://127.0.0.1:8765/mcp", lifecycle=LegacyGateway())
        async with server.session_manager.run():
            async with configured.connect() as client:
                self.assertEqual((await client.inspect())["protocolVersion"], "2025-11-25")
                self.assertEqual((await client.read_resource(URI))["contents"][0]["text"], "older retained")

    async def test_adk_approval_still_blocks_modern_tool_call(self):
        from dataclasses import replace
        from harnest.agent.approval import ApprovalPolicy

        await self.configured._lifecycle_binding("langgraph").close(reset=True)
        configured = replace(self.configured, approval=ApprovalPolicy("Approve echo"))
        binding = configured._lifecycle_binding("adk")
        await binding.start()
        toolset = configured.to_adk_toolset()
        try:
            tools = await toolset.get_tools()
            with patch("harnest.mcp.authorize_mcp", side_effect=PermissionError("denied")):
                with self.assertRaises(PermissionError):
                    await tools[0].run_async(args={"text": "blocked"}, tool_context=None)
            self.assertNotIn("tools/call", [message["method"] for _, message in self.gateway.requests])
        finally:
            await toolset.close()
            await binding.close(reset=True)

    async def test_real_http_server_retained_resource(self):
        import uvicorn

        gateway = self.gateway

        async def app(scope, receive, send):
            body = b""
            while True:
                incoming = await receive()
                body += incoming.get("body", b"")
                if not incoming.get("more_body"):
                    break
            request = httpx.Request("POST", "https://modern.invalid", headers=scope["headers"], content=body)
            response = gateway.respond(request)
            await send({"type": "http.response.start", "status": response.status_code, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": response.content})

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error", ws="none"))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            with anyio.fail_after(5):
                while not server.started:
                    await asyncio.sleep(.01)
                configured = MCPClient.streamable_http(f"http://127.0.0.1:{listener.getsockname()[1]}")
                async with configured.connect() as client:
                    self.assertEqual((await client.inspect())["protocolVersion"], VERSION)
                    self.assertEqual((await client.read_resource(URI))["contents"][0]["text"], "retained")
                await _managed_agent(configured)
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)
            listener.close()

    def test_header_encoding_and_schema_validation(self):
        self.assertEqual(tool_headers(SCHEMA, {"text": "normal"}), {"Mcp-Param-Text": "normal"})
        self.assertEqual(tool_headers(SCHEMA, {}), {})
        self.assertTrue(header_value("unsafe\r\nvalue").startswith("=?base64?"))
        self.assertTrue(header_value("é").startswith("=?base64?"))
        self.assertTrue(header_value("=?base64?literal?=").startswith("=?base64?PT"))
        for schema in ({"x-mcp-header": "Root"}, {"items": SCHEMA}, {"properties": {"a": {"type": "string", "x-mcp-header": "bad\r\n"}}}):
            self.assertFalse(valid_tool_schema(schema))


async def _managed_agent(configured):
    """Exercise the native agent loop against the running modern-only HTTP server."""

    from dataclasses import replace
    from langgraph.checkpoint.memory import MemorySaver
    from harnest.agent import AgentDefinition
    from harnest.application import CompiledApplication
    from harnest.backends.langgraph import ManagedAgentPlan
    from harnest.runtime_contract import InvocationRequest
    from harnest.runtime_langgraph import LangGraphRuntimeDriver
    from test_mcp_agent import _DiscoveryModel

    configured = replace(configured, identity="knowledge", capability_id="knowledge")
    definition = AgentDefinition(name="modern_agent", model=_DiscoveryModel(), instruction="Discover context", mcp=(configured,))
    application = CompiledApplication(name="modern_agent", framework="langgraph", mode="managed", target=ManagedAgentPlan(definition, checkpointer=MemorySaver()))
    driver = LangGraphRuntimeDriver(application)
    try:
        await driver.create_session(session_id="modern-session", user_id="tester", state={})
        result = await driver.invoke(InvocationRequest(input="Find context", user_id="tester", session_id="modern-session", invocation_id="modern-run", metadata={}, state_delta={}))
        if result.text != "retained / Summarize MCP":
            raise AssertionError(result.text)
    finally:
        await driver.close()
