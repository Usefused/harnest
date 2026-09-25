"""Exercise the private-input boundary through real framework and HTTP paths."""

import asyncio
import inspect
import pickle
import traceback
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from google.adk.apps import App
from google.adk.models import BaseLlm, LlmResponse
from google.genai import types
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, Field

from harnest.agent import Agent, AgentRuntimePrincipal, client_input, client_tool, tool
from harnest.agent.approval import ApprovalRun, require_human_approval
from harnest.agent_principal import activate_agent_principal, create_agent_principal_binding
from harnest.application import CompiledApplication
from harnest.backends.langgraph import lower_agent
from harnest.client_tool import ClientToolError, ClientToolExecution, InMemoryClientToolStore, client_tool_execution
from harnest.durable import ResumeArtifact, native_durable_call
from harnest.neutral_runtime import create_neutral_app
from harnest.runtime_adk import ADKRuntimeDriver
from harnest.runtime_contract import InvocationRequest, InvocationResult
from harnest.runtime_langgraph import LangGraphRuntimeDriver

from test_agui import _events
from test_neutral_runtime import FakeDriver, HeaderAuthenticator


SECRET = "private-client-sentinel-9bf872"


class PrivateFields(BaseModel):
    credential: str


class InputADKModel(BaseLlm):
    requests: list[Any] = Field(default_factory=list)

    async def generate_content_async(self, llm_request, stream=False):
        """Request private collection and capture the following provider context."""

        self.requests.append(str(llm_request))
        if len(self.requests) == 1:
            part = types.Part(function_call=types.FunctionCall(name="connect", args={"service": "mail"}))
        else:
            part = types.Part(text="connected")
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


class InputGraphModel(FakeMessagesListChatModel):
    requests: list[Any] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        """Capture advertised inputs as well as actual provider messages."""

        self.requests.append(str([item.args for item in tools]))
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.requests.append(str(messages))
        return super()._generate(messages, stop, run_manager, **kwargs)


def private_tool(observed, *, mode="ok"):
    """Keep the declared public response independent of the consumer's input."""

    @client_input(input_schema=PrivateFields, response={"status": "connected"})
    async def connect(private: PrivateFields, service: str) -> None:
        """Connect the requested service using private client credentials."""

        observed.append((private.credential, service))
        if mode == "raise":
            raise ValueError(private.credential)
        if mode == "return":
            return private

    return connect


def native_driver(framework, operation):
    """Construct native stores so persisted framework data can be inspected."""

    checkpoint = InMemorySaver()
    if framework == "adk":
        model = InputADKModel(model="offline")
        target = Agent(name="private_reader", model=model, instruction="Connect the service.", tools=[operation]).build()
        app = CompiledApplication(name="private_reader", framework="adk", mode="managed",
                                  target=target, native_app=App(name="private_reader", root_agent=target))
        return ADKRuntimeDriver(app), model, checkpoint
    model = InputGraphModel(responses=[AIMessage(content="", tool_calls=[
        {"name": "connect", "args": {"service": "mail"}, "id": "call"},
    ]), AIMessage(content="connected")])
    target = lower_agent(Agent(name="private_reader", model=model, instruction="Connect the service.", tools=[operation]),
                         checkpointer=checkpoint)
    app = CompiledApplication(name="private_reader", framework="langgraph", mode="managed", target=target)
    return LangGraphRuntimeDriver(app), model, checkpoint


class PrivateInputTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, operation, *, store=None):
        """Start an owned invocation and wait for its private request."""

        store = store or InMemoryClientToolStore()
        run = ApprovalRun("run", "alice", "session", "call")

        async def invoke():
            with client_tool_execution(ClientToolExecution(store, run)):
                return await operation(service="mail")

        task = asyncio.create_task(invoke())
        self.addAsyncCleanup(self.settle, task)
        run.task = task
        _, pending = await asyncio.wait_for(run.notifications.get(), 2)
        return store, pending, task

    async def settle(self, task):
        """Leave no suspended invocations behind after a failed assertion."""

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_private_delivery_is_scoped_one_time_and_not_serializable(self):
        observed = []
        operation = private_tool(observed)
        self.assertEqual(list(inspect.signature(operation).parameters), ["service"])
        store, pending, task = await self.start(operation)
        self.assertTrue(pending.public()["privateInput"])
        with self.assertRaises(KeyError):
            await store.submit(pending.id, user_id="bob", output={"credential": SECRET})
        with self.assertRaises(ClientToolError) as rejected:
            await store.submit(pending.id, user_id="alice", output={"credential": {"bad": SECRET}})
        self.assertIsNone(rejected.exception.__context__)
        self.assertNotIn(SECRET, str(rejected.exception))
        with self.assertLogs("harnest.agent.client_tool.audit", level="INFO") as logs:
            await store.submit(pending.id, user_id="alice", output={"credential": SECRET})
            delivery = pending.future.result()
            self.assertNotIn(SECRET, repr(delivery))
            with self.assertRaises(TypeError):
                pickle.dumps(delivery)
            with self.assertRaises(ClientToolError):
                await store.submit(pending.id, user_id="alice", output={"credential": SECRET})
            self.assertEqual(await task, {"status": "connected"})
        self.assertEqual(observed, [(SECRET, "mail")])
        self.assertIsNone(delivery._value)
        self.assertFalse(store.pending_for(user_id="alice", session_id="session"))
        self.assertEqual(store.transient_media.total_bytes, 0)
        self.assertNotIn(SECRET, str(logs.output))

    async def test_failures_returns_and_cancelled_delivery_cannot_leak(self):
        for mode in ("raise", "return", "cancel"):
            with self.subTest(mode=mode):
                observed = []
                store, pending, task = await self.start(private_tool(observed, mode=mode))
                await store.submit(pending.id, user_id="alice", output={"credential": SECRET})
                delivery = pending.future.result()
                if mode == "cancel":
                    self.assertTrue(task.cancel())
                with self.assertRaises((ClientToolError, asyncio.CancelledError)) as rejected:
                    await task
                self.assertIsNone(rejected.exception.__context__)
                self.assertNotIn(SECRET, "".join(traceback.format_exception(rejected.exception)))
                self.assertIsNone(delivery._value)

    async def test_decorators_and_durable_execution_fail_closed(self):
        operation = private_tool([])
        for wrapper in (tool, client_tool):
            with self.subTest(wrapper=wrapper):
                with self.assertRaises(TypeError):
                    wrapper(operation)
        with self.assertRaises(ClientToolError):
            await operation(service="mail")
        artifact = ResumeArtifact("langgraph", "thread", "tool", "connect")
        with native_durable_call(artifact):
            with self.assertRaisesRegex(ClientToolError, "durable"):
                await operation(service="mail")
        await self.check_approval_permissions()

    async def check_approval_permissions(self):
        """Permission markers must survive both approval decorator orders."""

        async def receive(private: PrivateFields, service: str) -> None:
            """Receive private credentials after application authorization."""

        decorate = client_input(input_schema=PrivateFields, response={"ok": True}, permission="accounts.connect")
        approve = require_human_approval(message="Approve connection?")
        binding = create_agent_principal_binding(AgentRuntimePrincipal.create(permissions={"accounts.connect"}))
        for operation in (approve(decorate(receive)), decorate(approve(receive))):
            with activate_agent_principal(binding), patch("harnest.agent.approval._authorize", AsyncMock(return_value=None)):
                store, pending, task = await self.start(operation)
                await store.submit(pending.id, user_id="alice", output={"credential": SECRET})
                self.assertEqual(await task, {"ok": True})

    async def test_cancelled_or_expired_request_cannot_deliver_private_input(self):
        for terminal in ("cancel", "expire"):
            with self.subTest(terminal=terminal):
                now = [0.0]
                observed = []
                store, pending, task = await self.start(private_tool(observed), store=InMemoryClientToolStore(clock=lambda: now[0]))
                if terminal == "cancel":
                    store.cancel(pending.id, user_id="alice")
                else:
                    now[0] = pending.expires_at
                with self.assertRaises(ClientToolError):
                    await store.submit(pending.id, user_id="alice", output={"credential": SECRET})
                await self.settle(task)
                self.assertEqual(observed, [])
                self.assertFalse(store.pending_for(user_id="alice", session_id="session"))

    async def test_native_model_context_history_and_checkpoints_never_receive_input(self):
        for framework in ("adk", "langgraph"):
            with self.subTest(framework=framework):
                await self.exercise_native(framework)

    async def exercise_native(self, framework):
        """Inspect real provider requests, events, sessions, and checkpoints."""

        observed = []
        driver, model, checkpoint = native_driver(framework, private_tool(observed))
        self.addAsyncCleanup(driver.close)
        await driver.create_session(session_id="session", user_id="alice", state={})
        request = InvocationRequest(input="connect mail", user_id="alice", session_id="session",
                                    invocation_id="call", metadata={}, state_delta={})
        store = InMemoryClientToolStore()
        run = ApprovalRun("run", "alice", "session", "call")

        async def invoke():
            with client_tool_execution(ClientToolExecution(store, run)):
                return [event async for event in driver.stream(request)]

        task = asyncio.create_task(invoke())
        self.addAsyncCleanup(self.settle, task)
        run.task = task
        _, pending = await asyncio.wait_for(run.notifications.get(), 5)
        await store.submit(pending.id, user_id="alice", output={"credential": SECRET})
        events = await asyncio.wait_for(task, 5)
        history = await driver.get_session_messages(session_id="session", user_id="alice")
        checkpoints = [item async for item in checkpoint.alist(None)]
        if framework == "adk":
            checkpoints.append(await driver._runner.session_service.get_session(
                app_name="private_reader", user_id="alice", session_id="session"))
        self.assertEqual(observed, [(SECRET, "mail")])
        for output in (events, history, checkpoints, model.requests):
            self.assertNotIn(SECRET, str(output))
            self.assertIn("connected", str(output))
        self.assertNotIn("PrivateFields", model.requests[0])


class PrivateDriver(FakeDriver):
    def __init__(self):
        super().__init__()
        self.observed = []
        self.operation = private_tool(self.observed)

    async def invoke(self, request):
        self.invocations.append(request)
        result = await self.operation(service="mail")
        return InvocationResult(text="connected", result=result, session_id=request.session_id,
                                events=({"type": "output", "value": result},), metadata={})

    async def stream(self, request):
        result = await self.invoke(request)
        for event in result.events:
            yield event


class PrivateInputAGUITests(unittest.TestCase):
    def test_json_and_live_transports_return_only_the_public_response(self):
        for transport in ("json", "live"):
            with self.subTest(transport=transport):
                driver = PrivateDriver()
                with TestClient(create_neutral_app(driver)) as client:
                    client.post("/sessions", json={"id": "private"})
                    completed = self.live_result(client) if transport == "live" else self.json_result(client)
                    self.assertEqual(completed["result"], {"status": "connected"})
                    self.assertNotIn(SECRET, str(completed))
                    self.assertEqual(driver.observed, [(SECRET, "mail")])

    def json_result(self, client):
        """Use the existing scoped result endpoint without a separate storage path."""

        response = client.post("/responses", json={"sessionId": "private", "input": "connect"}).json()
        action = response["requiredAction"]
        self.assertTrue(action["privateInput"])
        result = client.post(f"/client-tools/{action['id']}", json={"output": {"credential": SECRET}})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(client.post(f"/client-tools/{action['id']}", json={"output": {"credential": SECRET}}).status_code, 404)
        return result.json()

    def live_result(self, client):
        """Resolve a private prompt on its owning socket without echoing its value."""

        with client.websocket_connect("/live") as socket:
            socket.send_json({"type": "connect", "sessionId": "private"})
            socket.receive_json()
            socket.send_json({"type": "response.create", "input": "connect"})
            socket.receive_json()
            action = socket.receive_json()["clientTool"]
            self.assertTrue(action["privateInput"])
            socket.send_json({"type": "client_tool.result", "requestId": action["id"], "output": {"credential": SECRET}})
            while True:
                event = socket.receive_json()
                self.assertNotIn(SECRET, str(event))
                if event["type"] == "response.completed":
                    return event

    def test_private_resumes_reject_transcripts_and_publish_only_authored_response(self):
        driver = PrivateDriver()
        with TestClient(create_neutral_app(driver, authenticator=HeaderAuthenticator())) as client:
            headers = {"x-test-user": "alice"}
            initial = client.post("/agui", headers=headers, json={
                "threadId": "private", "messages": [{"role": "user", "content": "connect"}],
            })
            events = _events(initial.text)
            action = events[-1]["outcome"]["interrupts"][0]
            self.assertEqual(action["reason"], "private_input")
            self.assertFalse(any(event["type"] == "TOOL_CALL_START" for event in events))
            envelope = {"threadId": "private", "resume": [{
                "interruptId": action["id"], "status": "resolved", "payload": {"credential": SECRET},
            }]}
            wrong_user = client.post("/agui", headers={"x-test-user": "bob"}, json=envelope)
            self.assertEqual(wrong_user.status_code, 409)
            for extra in ({"messages": [{"role": "tool", "content": SECRET}]}, {"state": {"token": SECRET}}):
                rejected = client.post("/agui", headers=headers, json={**envelope, **extra})
                self.assertEqual(rejected.status_code, 400)
                self.assertNotIn(SECRET, rejected.text)
            resumed = client.post("/agui", headers=headers, json=envelope)
            self.assertEqual(_events(resumed.text)[-1]["result"], {"status": "connected"})
            self.assertNotIn(SECRET, resumed.text)
            self.assertEqual(driver.observed, [(SECRET, "mail")])
            self.assertEqual(client.post("/agui", headers=headers, json=envelope).status_code, 409)
            status = client.get(f"/responses/{action['metadata']['harnest']['callId']}",
                                params={"sessionId": "private"}, headers=headers)
            self.assertNotIn(SECRET, status.text)
            self.assertNotIn(SECRET, str(driver.invocations))
