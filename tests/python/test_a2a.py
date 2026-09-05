import asyncio
from dataclasses import replace
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from a2a import helpers
from a2a.auth.user import User
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.types import (
    ListTasksRequest,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    Task,
    TaskState,
    TaskStatus,
)
from a2a.utils.errors import InvalidParamsError
from fastapi.testclient import TestClient
from google.protobuf.json_format import MessageToDict

from harnest.a2a import A2AClient, A2AClientError
from harnest.checkpoint import MemoryStore
from harnest.external_continuation import PendingExternalContinuation
from harnest.neutral_runtime import create_neutral_app
from harnest.runtime_a2a_store import HarnestA2ATaskStore
from harnest.runtime_auth import ANONYMOUS_USER_ID

from test_neutral_runtime import (
    ApprovalDriver,
    FakeDriver,
    HeaderAuthenticator,
)


def _card(*, binding="HTTP+JSON", host="127.0.0.1:8080"):
    path = "/a2a"
    return {
        "name": "A2A Fixture",
        "description": "A deterministic A2A fixture.",
        "version": "1.0.0",
        "supportedInterfaces": [
            {
                "url": f"http://{host}{path}",
                "protocolBinding": binding,
                "protocolVersion": "1.0",
            }
        ],
        "capabilities": {"streaming": True},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": [
            {
                "id": "respond",
                "name": "Respond",
                "description": "Returns deterministic fixture output.",
                "tags": ["fixture"],
            }
        ],
    }


def _driver(driver_type=FakeDriver, **card_options):
    driver = driver_type()
    driver.info = replace(driver.info, card=_card(**card_options))
    return driver


def _send_payload(
    value="hello",
    *,
    context_id=None,
    task_id=None,
    return_immediately=False,
):
    message = helpers.new_text_message(value, role=Role.ROLE_USER)
    if context_id is not None:
        message.context_id = context_id
    if task_id is not None:
        message.task_id = task_id
    request = SendMessageRequest(
        message=message,
        configuration=SendMessageConfiguration(
            history_length=0,
            return_immediately=return_immediately,
        ),
    )
    return MessageToDict(request)


def _headers(user_id=None):
    headers = {
        "A2A-Version": "1.0",
        "Content-Type": "application/a2a+json",
    }
    if user_id is not None:
        headers["x-test-user"] = user_id
    return headers


class _TaskOwner(User):
    """Provide one stable principal to the official call-context model."""

    def __init__(self, user_id):
        self._user_id = user_id

    @property
    def is_authenticated(self):
        return True

    @property
    def user_name(self):
        return self._user_id


def _call_context(user_id=ANONYMOUS_USER_ID):
    """Build the minimum official context consumed by the task-store adapter."""

    return ServerCallContext(user=_TaskOwner(user_id))


def _working_task(task_id="durable-task", context_id="durable-context"):
    """Create one protocol-valid nonterminal snapshot for recovery tests."""

    return Task(
        id=task_id,
        context_id=context_id,
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
    )


class _DurablePollRuntime:
    """Expose a task wait whose final result may be observed by another executor."""

    def __init__(
        self, *, complete=False, cancelable=False, requires_action=False
    ):
        self.complete = complete
        self.cancelable = cancelable
        self.requires_action = requires_action
        self.cancel_scopes = []

    async def response_boundary(self, **_scope):
        """Return only the public boundary used by invocation polling."""

        if not self.complete:
            return (
                "external_continuation",
                PendingExternalContinuation("continuation-1", "task.result"),
            )
        if self.requires_action:
            return (
                "final",
                {
                    "id": "durable-task",
                    "sessionId": "durable-context",
                    "status": "requires_action",
                    "requiredAction": {
                        "type": "client_tool",
                        "id": "process-local-action",
                    },
                    "outputText": "",
                    "output": [],
                    "metadata": {},
                },
            )
        return (
            "final",
            {
                "id": "durable-task",
                "sessionId": "durable-context",
                "status": "completed",
                "outputText": "durable result",
                "output": [],
                "metadata": {},
            },
        )

    async def cancel_task_wait(self, **scope):
        """Record exact durable ownership before reporting cancellation."""

        self.cancel_scopes.append(scope)
        return self.cancelable


def _sse_payloads(response):
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def _harnest_status_update(events, event_type):
    """Find one namespaced Harnest activity update in an A2A stream."""

    return next(
        item["statusUpdate"]
        for item in events
        if item.get("statusUpdate", {})
        .get("metadata", {})
        .get("harnest", {})
        .get("type")
        == event_type
    )


def _usage_artifact_update(events):
    """Find the final artifact chunk carrying aggregate token usage."""

    return next(
        item["artifactUpdate"]
        for item in reversed(events)
        if item.get("artifactUpdate", {})
        .get("artifact", {})
        .get("metadata", {})
        .get("harnest", {})
        .get("usage")
    )


class BlockingA2ADriver(FakeDriver):
    """Keep one streaming execution live until A2A cancellation reaches it."""

    def __init__(self):
        super().__init__()
        self.cancelled = False

    async def stream(self, request):
        del request
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if False:  # pragma: no cover - keeps this an async generator
            yield {}


class A2AServerTests(unittest.TestCase):
    def test_blocking_send_returns_direct_message_without_task_state(self):
        driver = _driver()
        with TestClient(
            create_neutral_app(driver, playground_enabled=False)
        ) as client:
            response = client.post(
                "/a2a/message:send",
                json=_send_payload(),
                headers=_headers(),
            )
            page = client.get("/a2a/tasks", headers=_headers()).json()

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("task", response.json())
        self.assertEqual(response.json()["message"]["parts"][0]["text"], "hello")
        self.assertEqual(page["tasks"], [])
        self.assertEqual(page["nextPageToken"], "")

    def test_streaming_send_persists_owner_scoped_task_and_artifacts(self):
        driver = _driver()
        app = create_neutral_app(
            driver,
            playground_enabled=False,
            authenticator=HeaderAuthenticator(),
        )
        with TestClient(app) as client:
            stream = client.post(
                "/a2a/message:stream",
                json=_send_payload(),
                headers=_headers("alice"),
            )
            events = _sse_payloads(stream)
            task_id = events[0]["task"]["id"]
            owned = client.get(
                f"/a2a/tasks/{task_id}", headers=_headers("alice")
            )
            hidden = client.get(
                f"/a2a/tasks/{task_id}", headers=_headers("bob")
            )
            page = client.get("/a2a/tasks", headers=_headers("alice")).json()

        self.assertEqual(events[0]["task"]["status"]["state"], "TASK_STATE_SUBMITTED")
        self.assertEqual(
            events[-1]["statusUpdate"]["status"]["state"],
            "TASK_STATE_COMPLETED",
        )
        self.assertEqual(owned.status_code, 200)
        self.assertTrue(owned.json()["artifacts"])
        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(page["tasks"][0].get("artifacts", []), [])

    def test_streaming_send_projects_thinking_and_agent_activity(self):
        class ActivityDriver(FakeDriver):
            def events(self):
                return [
                    {
                        "type": "agent_activity",
                        "agent": "planner",
                        "activity": "started",
                        "nativeState": {"secret": True},
                    },
                    {
                        "type": "thinking",
                        "agent": "planner",
                        "text": "check constraints",
                        "signature": "private-signature",
                    },
                    {
                        "type": "agent_metadata",
                        "framework": "adk",
                        "agent": "planner",
                        "usage": {
                            "input_tokens": 12,
                            "output_tokens": 4,
                            "total_tokens": 17,
                        },
                        "raw": {"thoughts_token_count": 1},
                        "_raw_provider_metadata": True,
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "agent": "planner",
                        "text": "hello",
                    },
                ]

        with TestClient(
            create_neutral_app(
                _driver(ActivityDriver), playground_enabled=False
            )
        ) as client:
            direct = client.post(
                "/a2a/message:send",
                json=_send_payload("direct"),
                headers=_headers(),
            )
            response = client.post(
                "/a2a/message:stream",
                json=_send_payload(),
                headers=_headers(),
            )
            events = _sse_payloads(response)

        self.assertEqual(
            direct.json()["message"]["metadata"]["harnest"]["usage"],
            {
                "inputTokens": 12,
                "outputTokens": 4,
                "totalTokens": 17,
            },
        )

        activity = _harnest_status_update(events, "agent_activity")
        thinking = _harnest_status_update(events, "thinking")
        metadata = _harnest_status_update(events, "agent_metadata")
        artifact = next(
            item["artifactUpdate"] for item in events if "artifactUpdate" in item
        )
        usage_artifact = _usage_artifact_update(events)
        self.assertEqual(activity["metadata"]["harnest"]["activity"], "started")
        self.assertNotIn("nativeState", str(activity))
        self.assertEqual(
            thinking["status"]["message"]["parts"][0]["text"],
            "check constraints",
        )
        self.assertNotIn("signature", str(thinking))
        self.assertEqual(
            metadata["metadata"]["harnest"],
            {
                "type": "agent_metadata",
                "agent": "planner",
                "framework": "adk",
                "usage": {
                    "inputTokens": 12,
                    "outputTokens": 4,
                    "totalTokens": 17,
                },
                "raw": {"thoughts_token_count": 1},
            },
        )
        self.assertEqual(
            usage_artifact["artifact"]["metadata"]["harnest"]["usage"],
            {
                "inputTokens": 12,
                "outputTokens": 4,
                "totalTokens": 17,
            },
        )
        self.assertEqual(
            artifact["artifact"]["metadata"]["harnest"]["agent"], "planner"
        )

    def test_nonblocking_task_can_be_explicitly_canceled(self):
        driver = _driver(BlockingA2ADriver)
        with TestClient(
            create_neutral_app(driver, playground_enabled=False)
        ) as client:
            submitted = client.post(
                "/a2a/message:send",
                json=_send_payload(return_immediately=True),
                headers=_headers(),
            )
            task_id = submitted.json()["task"]["id"]
            canceled = client.post(
                f"/a2a/tasks/{task_id}:cancel",
                json={"id": task_id},
                headers=_headers(),
            )

        self.assertEqual(submitted.status_code, 200)
        self.assertEqual(canceled.status_code, 200)
        self.assertEqual(
            canceled.json()["status"]["state"], "TASK_STATE_CANCELED"
        )
        self.assertTrue(driver.cancelled)

    def test_task_directed_operations_preflight_principal_ownership(self):
        driver = _driver(BlockingA2ADriver)
        with TestClient(
            create_neutral_app(
                driver,
                playground_enabled=False,
                authenticator=HeaderAuthenticator(),
            )
        ) as client:
            submitted = client.post(
                "/a2a/message:send",
                json=_send_payload(return_immediately=True),
                headers=_headers("alice"),
            )
            task_id = submitted.json()["task"]["id"]
            hidden = client.post(
                f"/a2a/tasks/{task_id}:cancel",
                json={"id": task_id},
                headers=_headers("bob"),
            )
            canceled = client.post(
                f"/a2a/tasks/{task_id}:cancel",
                json={"id": task_id},
                headers=_headers("alice"),
            )

        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(canceled.status_code, 200)

    def test_human_approval_promotes_and_resumes_the_same_task(self):
        driver = _driver(ApprovalDriver)
        with TestClient(
            create_neutral_app(driver, playground_enabled=False)
        ) as client:
            blocked = client.post(
                "/a2a/message:send",
                json=_send_payload("protected"),
                headers=_headers(),
            ).json()["task"]
            decision = helpers.new_data_message(
                {"decision": "approve"},
                role=Role.ROLE_USER,
                task_id=blocked["id"],
                context_id=blocked["contextId"],
            )
            resumed = client.post(
                "/a2a/message:send",
                json=MessageToDict(
                    SendMessageRequest(
                        message=decision,
                        configuration=SendMessageConfiguration(history_length=0),
                    )
                ),
                headers=_headers(),
            )

        self.assertEqual(
            blocked["status"]["state"], "TASK_STATE_INPUT_REQUIRED"
        )
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(
            resumed.json()["task"]["status"]["state"],
            "TASK_STATE_COMPLETED",
        )
        self.assertEqual(driver.before_protected, 1)
        self.assertEqual(driver.after_protected, 1)

    def test_jsonrpc_binding_uses_the_same_direct_message_policy(self):
        driver = _driver(binding="JSONRPC")
        request = {
            "jsonrpc": "2.0",
            "id": "fixture",
            "method": "SendMessage",
            "params": _send_payload(),
        }
        with TestClient(
            create_neutral_app(driver, playground_enabled=False)
        ) as client:
            response = client.post(
                "/a2a",
                json=request,
                headers={"A2A-Version": "1.0"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["message"]["role"], "ROLE_AGENT")

    def test_server_rejects_capabilities_it_cannot_fulfill(self):
        for card_options, message in (
            ({"binding": "GRPC"}, "gRPC"),
            ({"binding": "HTTP+JSON"}, "push notifications"),
        ):
            with self.subTest(message=message):
                driver = _driver(**card_options)
                if message == "push notifications":
                    driver.info.card["capabilities"]["pushNotifications"] = True
                with self.assertRaisesRegex(ValueError, message):
                    create_neutral_app(driver, playground_enabled=False)

    def test_fresh_executor_refreshes_a_shared_durable_task(self):
        store = MemoryStore()
        adapter = HarnestA2ATaskStore(store, application_id="fixture")
        asyncio.run(adapter.save(_working_task(), _call_context()))
        polling = _DurablePollRuntime(complete=True)
        driver = _driver()
        driver.external_continuations = polling
        with TestClient(
            create_neutral_app(
                driver,
                playground_enabled=False,
                a2a_task_store=adapter,
            )
        ) as client:
            response = client.get(
                "/a2a/tasks/durable-task",
                headers=_headers(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["status"]["state"], "TASK_STATE_COMPLETED"
        )
        self.assertEqual(
            response.json()["artifacts"][0]["parts"][0]["text"],
            "durable result",
        )

    def test_subscription_polls_shared_durable_completion(self):
        store = MemoryStore()
        adapter = HarnestA2ATaskStore(store, application_id="fixture")
        asyncio.run(adapter.save(_working_task(), _call_context()))
        polling = _DurablePollRuntime(complete=True)
        driver = _driver()
        driver.external_continuations = polling
        with TestClient(
            create_neutral_app(
                driver,
                playground_enabled=False,
                a2a_task_store=adapter,
            )
        ) as client:
            response = client.get(
                "/a2a/tasks/durable-task:subscribe",
                headers=_headers(),
            )
            events = _sse_payloads(response)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(events[0]["task"]["status"]["state"], "TASK_STATE_WORKING")
        self.assertEqual(
            events[-1]["task"]["status"]["state"], "TASK_STATE_COMPLETED"
        )

    def test_fresh_executor_does_not_advertise_unresumable_input(self):
        store = MemoryStore()
        adapter = HarnestA2ATaskStore(store, application_id="fixture")
        asyncio.run(adapter.save(_working_task(), _call_context()))
        polling = _DurablePollRuntime(complete=True, requires_action=True)
        driver = _driver()
        driver.external_continuations = polling
        with TestClient(
            create_neutral_app(
                driver,
                playground_enabled=False,
                a2a_task_store=adapter,
            )
        ) as client:
            response = client.get(
                "/a2a/tasks/durable-task",
                headers=_headers(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["status"]["state"], "TASK_STATE_FAILED"
        )

    def test_cancel_delegates_an_awaited_task_to_durable_ownership(self):
        store = MemoryStore()
        adapter = HarnestA2ATaskStore(store, application_id="fixture")
        asyncio.run(adapter.save(_working_task(), _call_context()))
        polling = _DurablePollRuntime(cancelable=True)
        driver = _driver()
        driver.external_continuations = polling
        with TestClient(
            create_neutral_app(
                driver,
                playground_enabled=False,
                a2a_task_store=adapter,
            )
        ) as client:
            # Durable ownership must not depend on the SDK registry retaining
            # the producer that originally suspended into the external wait.
            with patch.object(
                DefaultRequestHandler,
                "on_cancel_task",
                new=AsyncMock(side_effect=AssertionError("SDK path used")),
            ):
                response = client.post(
                    "/a2a/tasks/durable-task:cancel",
                    json={"id": "durable-task"},
                    headers=_headers(),
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["status"]["state"], "TASK_STATE_CANCELED"
        )
        self.assertEqual(
            polling.cancel_scopes,
            [
                {
                    "response_id": "durable-task",
                    "user_id": ANONYMOUS_USER_ID,
                    "session_id": "durable-context",
                }
            ],
        )

class A2ATaskStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.persistence = MemoryStore()
        await self.persistence.start()
        self.store = HarnestA2ATaskStore(
            self.persistence, application_id="fixture"
        )
        self.alice = _call_context("alice")

    async def asyncTearDown(self):
        await self.persistence.close()

    async def test_owner_scoping_and_inclusive_pagination(self):
        for task_id in ("task-a", "task-b", "task-c"):
            await self.store.save(_working_task(task_id), self.alice)

        first = await self.store.list(
            ListTasksRequest(page_size=2, include_artifacts=True), self.alice
        )
        second = await self.store.list(
            ListTasksRequest(
                page_size=2,
                page_token=first.next_page_token,
                include_artifacts=True,
            ),
            self.alice,
        )
        hidden = await self.store.get("task-a", _call_context("bob"))

        self.assertEqual(first.total_size, 3)
        self.assertEqual(len(first.tasks), 2)
        self.assertTrue(first.next_page_token)
        self.assertEqual(len(second.tasks), 1)
        self.assertFalse(second.next_page_token)
        self.assertIsNone(hidden)

    async def test_context_rebinding_and_invalid_cursors_fail_closed(self):
        await self.store.save(_working_task("task-a", "context-a"), self.alice)

        with self.assertRaises(InvalidParamsError):
            await self.store.save(
                _working_task("task-a", "context-b"), self.alice
            )
        for token in ("not-a-token", "%%%%"):
            with self.subTest(token=token), self.assertRaises(InvalidParamsError):
                await self.store.list(
                    ListTasksRequest(page_token=token), self.alice
                )


class A2AClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_is_lazy_and_uses_direct_message_by_default(self):
        driver = _driver(host="testserver")
        app = create_neutral_app(driver, playground_enabled=False)
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        client = A2AClient(
            "http://testserver/.well-known/agent-card.json",
            allow_insecure=True,
            allowed_hosts=("testserver",),
            http_client=http,
        )
        self.assertIsNone(client.card)

        result = await client.send("hello")
        page = await client.list_tasks()
        await client.close()

        self.assertEqual(result.text, "hello")
        self.assertEqual(result.data, 42.0)
        self.assertIsNone(result.task_id)
        self.assertEqual(list(page.tasks), [])

    async def test_client_streaming_is_explicit(self):
        driver = _driver(host="testserver")
        app = create_neutral_app(driver, playground_enabled=False)
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        client = A2AClient(
            "http://testserver/.well-known/agent-card.json",
            streaming=True,
            allow_insecure=True,
            allowed_hosts=("testserver",),
            http_client=http,
        )

        updates = [update async for update in client.stream("hello")]
        await client.close()

        self.assertEqual(updates[0].kind, "task")
        self.assertEqual(updates[-1].state, "completed")
        self.assertTrue(any(update.text == "hel" for update in updates))

    async def test_client_resumes_an_input_required_task_explicitly(self):
        driver = _driver(ApprovalDriver, host="testserver")
        app = create_neutral_app(driver, playground_enabled=False)
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        client = A2AClient(
            "http://testserver/.well-known/agent-card.json",
            allow_insecure=True,
            allowed_hosts=("testserver",),
            http_client=http,
        )

        blocked = await client.send("protected")
        completed = await client.send(
            {"decision": "approve"},
            context_id=blocked.context_id,
            task_id=blocked.task_id,
        )
        await client.close()

        self.assertEqual(blocked.state, "input_required")
        self.assertEqual(blocked.data["type"], "human_approval")
        self.assertEqual(completed.state, "completed")
        self.assertEqual(driver.before_protected, 1)
        self.assertEqual(driver.after_protected, 1)

    async def test_client_rejects_discovered_cross_host_interfaces(self):
        card = _card(host="other.example")
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(200, json=card)
        )
        http = httpx.AsyncClient(transport=transport)
        client = A2AClient(
            "https://agent.example/.well-known/agent-card.json",
            http_client=http,
        )

        with self.assertRaisesRegex(A2AClientError, "network policy"):
            await client.connect()
        await client.close()

    async def test_streaming_requires_explicit_client_policy(self):
        client = A2AClient("https://agent.example/.well-known/agent-card.json")
        with self.assertRaisesRegex(A2AClientError, "streaming=True"):
            await anext(client.stream("hello"))
        await client.close()


if __name__ == "__main__":
    unittest.main()
