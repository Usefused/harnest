"""Wire-level subscriptions/listen, retained recovery, and shutdown contracts."""

import asyncio
from contextvars import ContextVar
import json
import socket
import unittest

import httpx

from harnest.mcp import MCPClient, MCPClientLifecycle, MCPSubscription
from harnest.mcp_lifecycle import close_mcp_lifecycles, start_mcp_lifecycles
from harnest.mcp_subscription_http import _messages
from harnest.mcp_subscriptions import _ResourceListener

URI = "events://orders/latest"
META = "io.modelcontextprotocol/subscriptionId"
PRIVATE = ContextVar("private_test_request", default=None)


def notification(method, request_id, **params):
    message = {"jsonrpc": "2.0", "method": method, "params": {"_meta": {META: request_id}, **params}}
    return f"data: {json.dumps(message)}\n\n".encode()


class _SubscriptionStream(httpx.AsyncByteStream):
    def __init__(self, server, request_id):
        self.server, self.request_id = server, request_id

    async def __aiter__(self):
        accepted = [] if self.server.reject else [URI]
        yield notification("notifications/subscriptions/acknowledged", self.request_id, notifications={"resourceSubscriptions": accepted})
        if self.server.connections == 1:
            self.server.version = 1
            yield notification("notifications/resources/updated", "wrong-id", uri=URI)
            yield notification("notifications/resources/updated", self.request_id, uri=URI)
            self.server.version = 2
            raise httpx.ReadError("private gateway token must not be logged")
        await asyncio.Event().wait()

    async def aclose(self):
        self.server.closed += 1


class _Gateway(MCPClientLifecycle):
    def __init__(self, *, reject=False):
        self.version, self.connections, self.closed = 0, 0, 0
        self.reject = reject
        self.requests = []
        self.lifecycle = []

    async def start(self, context):
        self.lifecycle.append("start")

    async def close(self, context):
        self.lifecycle.append("close")

    def create_http_client(self, options, context):
        self.lifecycle.append("client")
        return httpx.AsyncClient(transport=httpx.MockTransport(self.respond), headers=options.headers)

    async def respond(self, request):
        message = json.loads(request.content)
        self.requests.append((request.headers, message))
        method = message["method"]
        if method == "subscriptions/listen":
            self.connections += 1
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_SubscriptionStream(self, message["id"]))
        result = {"capabilities": {"resources": {"subscribe": True}}}
        if method == "resources/read":
            result = {"contents": [{"uri": URI, "text": str(self.version)}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": {"resultType": "complete", **result}})


class MCPSubscriptionHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_http_stream_recovers_retained_value_after_disconnect(self):
        """Recover retained values over real HTTP despite ordinary runner contention."""
        import uvicorn

        # This exercises recovery, not latency; dedicated protocol tests enforce
        # RPC deadlines. Allow slow CI scheduling while keeping every wait bounded.
        timeout = 30
        application = _live_application()
        listener_socket = socket.socket()
        listener_socket.bind(("127.0.0.1", 0))
        port = listener_socket.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(application, log_level="error", lifespan="off", ws="none"))
        server_task = asyncio.create_task(server.serve(sockets=[listener_socket]))
        received = []
        recovered = asyncio.Event()

        async def handler(event):
            received.append(event.resource["contents"][0]["text"])
            if event.reason == "reconnect":
                recovered.set()

        configured = MCPClient.streamable_http(f"http://127.0.0.1:{port}/mcp", timeout_seconds=timeout,
            subscriptions=[MCPSubscription(URI, handler, method="subscriptions/listen")])
        bindings = configured._runtime_bindings("langgraph")
        try:
            await asyncio.wait_for(_wait_started(server), timeout)
            await start_mcp_lifecycles(bindings)
            await asyncio.wait_for(recovered.wait(), timeout)
            self.assertEqual(received, ["0", "1", "2"])
        finally:
            await close_mcp_lifecycles(bindings)
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout)
            listener_socket.close()

    async def test_listen_ack_reconnect_read_and_cleanup_reuse_configured_gateway(self):
        gateway = _Gateway()
        events, contexts = [], []
        recovered = asyncio.Event()

        async def handler(event):
            contexts.append(PRIVATE.get())
            events.append(event)
            if event.reason == "reconnect":
                recovered.set()

        configured = MCPClient.streamable_http(
            "https://gateway.invalid/mcp", headers={"Authorization": "Bearer private"},
            lifecycle=gateway, subscriptions=[MCPSubscription(URI, handler, method="subscriptions/listen")],
        )
        bindings = configured._runtime_bindings("adk")
        token = PRIVATE.set("caller-private-session")
        try:
            await start_mcp_lifecycles(bindings)
            await asyncio.wait_for(recovered.wait(), 5)
        finally:
            await close_mcp_lifecycles(bindings)
            PRIVATE.reset(token)
        self.assertEqual([event.reason for event in events], ["start", "update", "reconnect"])
        self.assertEqual([event.resource["contents"][0]["text"] for event in events], ["0", "1", "2"])
        self.assertEqual(contexts, [None, None, None])
        self.assertEqual(gateway.closed, 2)
        self.assertEqual(gateway.lifecycle[-1], "close")
        for headers, message in gateway.requests:
            self.assertEqual(headers["Authorization"], "Bearer private")
            self.assertEqual(message["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"], "2026-07-28")

    async def test_unacknowledged_uri_fails_startup_and_unwinds_credentials(self):
        async def handler(event):
            self.fail("unacknowledged subscriptions must not call a handler")

        gateway = _Gateway(reject=True)
        configured = MCPClient.streamable_http("https://gateway.invalid/mcp", lifecycle=gateway,
            subscriptions=[MCPSubscription(URI, handler, method="subscriptions/listen")])
        with self.assertRaisesRegex(RuntimeError, "MCP lifecycle start failed"):
            await start_mcp_lifecycles(configured._runtime_bindings("langgraph"))
        self.assertEqual(gateway.lifecycle[-1], "close")
        self.assertEqual(gateway.closed, 1)

    async def test_handler_failure_does_not_commit_deduplication(self):
        attempts = []

        async def handler(event):
            attempts.append(event)
            if len(attempts) == 1:
                raise RuntimeError("private event payload")

        async def read():
            return {"contents": [{"uri": URI, "text": "retained"}]}

        configured = MCPClient.streamable_http("https://gateway.invalid/mcp")
        listener = _ResourceListener(configured, MCPSubscription(URI, handler), "langgraph")
        with self.assertRaises(RuntimeError):
            await listener.deliver(read, "update")
        self.assertIsNone(listener.digest)
        await listener.deliver(read, "reconnect")
        await listener.deliver(read, "update")
        self.assertEqual(len(attempts), 2)
        await listener.close()

    async def test_sse_rejects_oversized_frames(self):
        response = httpx.Response(200, content=b"data: " + b"x" * 2048 + b"\n\n", request=httpx.Request("POST", "https://gateway.invalid"))
        with self.assertRaisesRegex(RuntimeError, "max_content_bytes"):
            async for _ in _messages(response, 1024):
                self.fail("oversized content must not be decoded")


async def _wait_started(server):
    """Wait for socket registration without racing the first client connection."""

    while not server.started:
        await asyncio.sleep(0.01)


def _live_application():
    """Expose only disposable protocol data over an actual loopback HTTP socket."""

    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse

    app = FastAPI()
    state = {"version": 0, "connections": 0}
    reads = {0: asyncio.Event(), 1: asyncio.Event(), 2: asyncio.Event()}

    async def stream(request_id):
        yield notification("notifications/subscriptions/acknowledged", request_id, notifications={"resourceSubscriptions": [URI]})
        if state["connections"] == 1:
            await reads[0].wait()
            state["version"] = 1
            yield notification("notifications/resources/updated", request_id, uri=URI)
            await reads[1].wait()
            state["version"] = 2
            return
        await asyncio.Event().wait()

    @app.post("/mcp")
    async def endpoint(request: Request):
        message = await request.json()
        if message["method"] == "subscriptions/listen":
            state["connections"] += 1
            return StreamingResponse(stream(message["id"]), media_type="text/event-stream")
        result = {"capabilities": {"resources": {"subscribe": True}}}
        if message["method"] == "resources/read":
            result = {"contents": [{"uri": URI, "text": str(state["version"])}]}
            reads[state["version"]].set()
        return {"jsonrpc": "2.0", "id": message["id"], "result": {"resultType": "complete", **result}}

    return app
