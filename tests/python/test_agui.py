import asyncio
import json
import unittest
from unittest.mock import patch
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from harnest.neutral_runtime import (
    InvocationRequest,
    RuntimeEvent,
    create_neutral_app,
    create_neutral_router,
)
from harnest.runtime_agui import _AGUIEncoder, _json_pointer_escape
from harnest.agent.approval import InMemoryApprovalStore
from harnest.assets import MemoryAssetStore
from harnest.client_tool import InMemoryClientToolStore, client_tool
from harnest.runtime_invocation import InvocationCoordinator

from test_neutral_runtime import (
    ApprovalDriver,
    CancellableLiveDriver,
    ClientToolDriver,
    MultipleApprovalDriver,
    ApprovedClientToolDriver,
    CredentialClientToolDriver,
    CredentialHeaderAuthenticator,
    ExternalLiveDriver,
    _protected_send,
    _browser_open,
    FakeDriver,
    HeaderAuthenticator,
)


class StateDeltaDriver(FakeDriver):
    """Emit one state-changing turn ahead of the fixture's usual events."""

    async def stream(self, request: InvocationRequest) -> AsyncIterator[RuntimeEvent]:
        self.invocations.append(request)
        yield {"type": "state_delta", "delta": {"count": 1, "label": "hi"}}
        yield {"type": "message", "role": "assistant", "text": "ok"}


class BrowserResult(BaseModel):
    model_config = ConfigDict(strict=True)
    title: str


@client_tool(output_schema=BrowserResult)
def _typed_browser() -> BrowserResult:
    """Return the title of the page selected by the user."""

    raise AssertionError("declaration must not execute")


class TypedClientToolDriver(FakeDriver):
    async def stream(self, request):
        """Keep the invocation alive across rejected typed client outputs."""

        self.invocations.append(request)
        result = await _typed_browser()
        yield {"type": "message", "text": result["title"]}


class ParallelApprovalDriver(ApprovalDriver):
    """Suspend two protected calls in one invocation for batch-resume tests."""

    async def stream(self, request):
        self.before_protected += 1
        replies = await asyncio.gather(_protected_send("one"), _protected_send("two"))
        self.after_protected += 1
        yield {"type": "message", "text": ",".join(replies)}


class NativeClientToolDriver(FakeDriver):
    """Emit the native model call before the actual client-tool suspension."""

    async def stream(self, request):
        self.invocations.append(request)
        yield {"type": "tool_call", "id": "native-call", "name": "_browser_open", "arguments": {"url": request.input}}
        result = await _browser_open(request.input)
        yield {"type": "tool_result", "id": "native-call", "name": "_browser_open", "result": result}
        yield {"type": "message", "text": "opened"}
        yield {"type": "output", "value": result}


class SlowApprovalDriver(ApprovalDriver):
    """Make a resumed execution exceed the shared response timeout."""

    async def stream(self, request):
        await self.invoke(request)
        yield {"type": "message", "text": "approved"}
        await asyncio.sleep(10)


def _events(response_text: str) -> list[dict[str, Any]]:
    """Parse an AG-UI SSE body into its ordered list of decoded events."""

    return [
        json.loads(line[len("data: ") :])
        for line in response_text.splitlines()
        if line.startswith("data: ")
    ]


class AGUITestCase(unittest.TestCase):
    def _client(self, driver, **options):
        """Keep ASGI lifespan and pending-run cleanup scoped to each test."""

        return self.enterContext(TestClient(create_neutral_app(driver, **options)))

    def _post(self, client, *, headers=None, **payload):
        """Supply the common envelope while allowing malformed-field tests."""

        return client.post("/agui", headers=headers, json={
            "threadId": "thread", "runId": "next", "messages": [], **payload,
        })

    def _run(self, client, *, thread_id="thread-1", text="hello", **options):
        return self._post(client, threadId=thread_id, runId="run-1",
                          messages=[{"role": "user", "content": text}], **options)


class AGUIRuntimeTests(AGUITestCase):
    def setUp(self) -> None:
        self.driver = FakeDriver()
        self.client = self._client(self.driver)

    def test_agui_run_streams_lifecycle_text_and_tool_events(self):
        response = self._run(self.client)
        self.assertEqual(response.status_code, 200)
        events = _events(response.text)
        self.assertEqual(
            events[0],
            {"type": "RUN_STARTED", "threadId": "thread-1", "runId": "run-1"},
        )
        self.assertEqual(events[-1]["type"], "RUN_FINISHED")
        types = [event["type"] for event in events]
        self.assertEqual(
            types,
            [
                "RUN_STARTED",
                "TEXT_MESSAGE_START",
                "TEXT_MESSAGE_CONTENT",
                "TEXT_MESSAGE_CONTENT",
                "TEXT_MESSAGE_END",
                "TOOL_CALL_START",
                "TOOL_CALL_ARGS",
                "TOOL_CALL_END",
                "TOOL_CALL_RESULT",
                "RUN_FINISHED",
            ],
        )
        message_id = events[1]["messageId"]
        for event, delta in zip(events[2:4], ("hel", "lo"), strict=True):
            self.assertEqual(event, {"type": "TEXT_MESSAGE_CONTENT", "messageId": message_id, "delta": delta})
        self.assertEqual(events[5]["toolCallName"], "echo")
        self.assertEqual(json.loads(events[6]["delta"]), {"text": "hello"})
        self.assertEqual(events[7]["toolCallId"], events[5]["toolCallId"])
        self.assertEqual(events[8]["content"], "hello")
        second = _events(self._run(self.client, text="next").text)
        ids = [{e["messageId"] for e in turn if "messageId" in e} for turn in (events, second)]
        self.assertTrue(ids[0].isdisjoint(ids[1]))
        self.assertEqual(len(self.driver.sessions), 1)
        self.assertEqual([r.input for r in self.driver.invocations], ["hello", "next"])

    def test_agui_run_creates_an_implicit_thread_when_none_is_supplied(self):
        response = self.client.post(
            "/agui",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
        self.assertEqual(response.status_code, 200)
        events = _events(response.text)
        thread_id = events[0]["threadId"]
        self.assertTrue(thread_id)
        self.assertEqual(events[-1]["threadId"], thread_id)

    def test_empty_initialization_creates_thread_without_invoking_model(self):
        response = self.client.post(
            "/agui", json={"threadId": "thread-1", "messages": []}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([e["type"] for e in _events(response.text)], ["RUN_STARTED", "RUN_FINISHED"])
        self.assertFalse(self.driver.invocations)

    def test_agui_route_is_absent_when_disabled(self):
        app = create_neutral_app(FakeDriver(), agui_enabled=False)
        with TestClient(app) as client:
            self.assertEqual(client.post("/agui", json={}).status_code, 404)
            self.assertNotIn("agui", client.get("/agent").json()["endpoints"])

    def test_shared_pipeline_records_completed_and_failed_responses(self):
        """AG-UI must release polling receipts on both success and failure."""

        for fails in (False, True):
            with self.subTest(fails=fails):
                self.driver.fail_stream = fails
                response = self._run(self.client, thread_id=f"thread-{fails}")
                invocation = self.driver.invocations[-1]
                status = self.client.get(
                    f"/responses/{invocation.invocation_id}",
                    params={"sessionId": invocation.session_id},
                ).json()
                self.assertEqual(status["status"], "failed" if fails else "completed")
                events = _events(response.text)
                self.assertEqual(
                    events[-1]["type"], "RUN_ERROR" if fails else "RUN_FINISHED"
                )
                if not fails:
                    self.assertEqual(status["outputText"], "hello")

    def test_adapter_uses_configured_body_limit_before_creating_a_session(self):
        """Extra envelope data cannot bypass the host's request-size limit."""

        driver = FakeDriver()
        app = FastAPI()
        app.include_router(create_neutral_router(driver, max_request_bytes=1024))
        with TestClient(app) as client:
            response = self._run(client, text="hi", padding="x" * 2000)
        self.assertEqual(response.status_code, 413)
        self.assertFalse(driver.sessions)
        self.assertFalse(driver.invocations)

    def test_adapter_preserves_authentication_and_session_ownership(self):
        """Alternate encoding must not create another session authorization path."""

        driver = FakeDriver()
        client = self._client(driver, authenticator=HeaderAuthenticator())
        client.post("/sessions", headers={"x-test-user": "alice"}, json={"id": "private"})
        self.assertEqual(self._run(client, thread_id="private").status_code, 401)
        for user in ("bob", "alice"):
            response = self._run(client, thread_id="private", headers={"x-test-user": user})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(_events(response.text)[-1]["type"], "RUN_FINISHED")
            self.assertEqual(driver.invocations[-1].user_id, user)
            self.assertIn(("alice", "private"), driver.sessions)
            self.assertIn(("bob", "private"), driver.sessions)

    def test_timeout_cancels_work_closes_text_and_records_failure(self):
        """Timeout behavior and polling state are owned by the shared runner."""

        driver = CancellableLiveDriver()
        with TestClient(create_neutral_app(driver, request_timeout=0.15)) as client:
            response = self._run(client)
            invocation = driver.invocations[-1]
            status = client.get(
                f"/responses/{invocation.invocation_id}",
                params={"sessionId": invocation.session_id},
            ).json()
        events = _events(response.text)
        self.assertEqual(events[-2]["type"], "TEXT_MESSAGE_END")
        self.assertEqual(
            events[-1], {"type": "RUN_ERROR", "message": "Response timed out"}
        )
        self.assertEqual(driver.cancelled, 1)
        self.assertEqual(status["status"], "failed")

    def test_empty_output_uses_the_existing_response_failure_policy(self):
        """An empty agent result must not become an AG-UI success."""

        self.driver.empty_output = True
        response = self._run(self.client)
        self.assertEqual(_events(response.text)[-1]["type"], "RUN_ERROR")

    def test_state_context_and_tool_advertisements_reach_invocation(self):
        """Client context stays ordinary metadata and never sets identity."""

        payload = {
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            "state": {"filter": "open"},
            "context": [{"description": "Current page", "value": "orders"}],
            "tools": [{"name": "show_order", "parameters": {"type": "object"}}],
            "forwardedProps": {"user_id": "attacker"},
        }
        response = self.client.post("/agui", json=payload)
        self.assertEqual(response.status_code, 200)
        request = self.driver.invocations[-1]
        self.assertEqual(request.input, "hi")
        self.assertEqual(request.state_delta, payload["state"])
        self.assertEqual(request.metadata["agui"]["context"], payload["context"])
        self.assertEqual(request.metadata["agui"]["tools"], payload["tools"])
        self.assertNotEqual(request.user_id, "attacker")

    def test_invalid_envelopes_have_no_session_side_effects(self):
        """Validate optional fields as strictly as the message body."""

        cases = [
            {"threadId": " "}, {"runId": 12}, {"state": []}, {"state": {"_harnest_state": {}}},
            {"state": {"app:secret": "bad"}}, {"context": [{}]}, {"tools": [{}]},
            {"messages": [{"role": "user", "content": " "}]},
            {"messages": [{"role": "user", "content": [{"type": "image", "url": "x"}]}]},
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                response = self.client.post("/agui", json={"messages": [{"role": "user", "content": "hi"}], **extra})
                self.assertEqual(response.status_code, 400, response.text)
        self.assertFalse(self.driver.sessions)
        self.assertFalse(self.driver.invocations)


class AGUIContinuationTests(AGUITestCase):
    """Exercise real shared suspension, authorization, and resumption over HTTP."""

    def _start(self, client, *, headers=None):
        response = self._post(client, headers=headers, runId="first",
                              messages=[{"id": "user-1", "role": "user", "content": "hello"}])
        self.assertEqual(response.status_code, 200, response.text)
        return _events(response.text)

    def _resume(self, client, *actions, payload=None, status="resolved", **options):
        """Resume one or a batch of actions using the same transport envelope."""

        reply = {} if status == "cancelled" else {"payload": {"approved": True} if payload is None else payload}
        return self._post(client, resume=[
            {"interruptId": action["id"], "status": status, **reply} for action in actions
        ], **options)

    def _client_action(self, events):
        """Read Harnest correlation without interrupting the SDK tool-result loop."""

        self.assertNotIn("outcome", events[-1])
        return next(e["value"] for e in events if e.get("name") == "harnest.client_tool")

    def test_approval_validates_then_resumes_original_work_once(self):
        driver = ApprovalDriver()
        client = self._client(driver)
        events = self._start(client)
        self.assertEqual(events[-1]["type"], "RUN_FINISHED")
        self.assertEqual(events[-1]["outcome"]["type"], "interrupt")
        self.assertEqual([e["type"] for e in events[-3:-1]], ["STATE_SNAPSHOT", "MESSAGES_SNAPSHOT"])
        interrupt = events[-1]["outcome"]["interrupts"][0]
        self.assertEqual(interrupt["reason"], "confirmation")
        self.assertEqual(self._resume(client, interrupt, payload={"approved": "yes"}).status_code, 400)
        self.assertEqual(driver.after_protected, 0)
        with patch.object(driver, "get_session", side_effect=AssertionError("live session lease")):
            resumed = _events(self._resume(client, interrupt).text)
        self.assertEqual(resumed[-1]["type"], "RUN_FINISHED")
        self.assertEqual(resumed[0]["runId"], "next")
        self.assertEqual("".join(e.get("delta", "") for e in resumed), "sent:hello")
        self.assertEqual((driver.before_protected, driver.after_protected), (1, 1))
        self.assertEqual(self._resume(client, interrupt).status_code, 409)
        self.assertEqual(driver.after_protected, 1)
        status = client.get(f"/responses/{interrupt['metadata']['harnest']['callId']}", params={"sessionId": "thread"}).json()
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["metadata"]["agui"]["context"], [])

    def test_denied_or_expired_approval_never_executes(self):
        import time

        for expired in (False, True):
            with self.subTest(expired=expired):
                now = [time.time()]
                driver = ApprovalDriver()
                with TestClient(create_neutral_app(driver, approval_store=InMemoryApprovalStore(clock=lambda: now[0]))) as client:
                    interrupt = self._start(client)[-1]["outcome"]["interrupts"][0]
                    now[0] += 1000 if expired else 0
                    response = self._resume(client, interrupt, payload={"approved": expired})
                    self.assertEqual(_events(response.text)[-1]["type"], "RUN_ERROR")
                    self.assertEqual(driver.after_protected, 0)

    def test_pending_approval_blocks_new_input_and_cross_scope_resume(self):
        driver = ApprovalDriver()
        client = self._client(driver, authenticator=HeaderAuthenticator())
        alice = {"x-test-user": "alice"}
        interrupt = self._start(client, headers=alice)[-1]["outcome"]["interrupts"][0]
        for headers, thread in [(alice, "different"), ({"x-test-user": "bob"}, "thread")]:
            self.assertEqual(self._resume(client, interrupt, headers=headers, threadId=thread).status_code, 409)
        self.assertEqual(self._run(client, thread_id="thread", text="bypass", headers=alice).status_code, 409)
        self.assertEqual((driver.before_protected, driver.after_protected), (1, 0))
        self.assertEqual(self._resume(client, interrupt, headers=alice).status_code, 200)

    def test_sequential_approvals_continue_without_restarting(self):
        driver = MultipleApprovalDriver()
        client = self._client(driver)
        events = self._start(client)
        for _ in range(2):
            events = _events(self._resume(client, events[-1]["outcome"]["interrupts"][0]).text)
        self.assertEqual(events[-1]["type"], "RUN_FINISHED")
        self.assertNotIn("outcome", events[-1])
        self.assertEqual((driver.before_protected, driver.after_protected), (1, 1))

    def test_client_tool_messages_resume_native_and_synthetic_calls(self):
        for driver_type, expected_text in [(ClientToolDriver, "opened:Example"), (NativeClientToolDriver, "opened")]:
            with self.subTest(driver=driver_type.__name__):
                driver = driver_type()
                with TestClient(create_neutral_app(driver)) as client:
                    initial = self._start(client)
                    action = self._client_action(initial)
                    starts = [e["toolCallId"] for e in initial if e["type"] == "TOOL_CALL_START"]
                    self.assertEqual(starts, [action["toolCallId"]])
                    with patch.object(driver, "get_session", side_effect=AssertionError("live session lease")):
                        events = _events(self._post(client, messages=[{
                            "role": "tool", "toolCallId": action["toolCallId"], "content": '{"title":"Example"}',
                        }]).text)
                    self.assertEqual(events[-1]["type"], "RUN_FINISHED")
                    self.assertEqual("".join(e.get("delta", "") for e in events), expected_text)
                    if driver_type is NativeClientToolDriver:
                        self.assertEqual(len(driver.invocations), 1)
                        self.assertEqual(action["toolCallId"], "native-call")
                        result = next(e for e in events if e["type"] == "TOOL_CALL_RESULT")
                        self.assertEqual(result["toolCallId"], "native-call")
                        self.assertEqual(events[-1]["result"], {"title": "Example"})

    def test_approved_client_tool_can_suspend_again(self):
        client = self._client(ApprovedClientToolDriver())
        approval = self._start(client)[-1]["outcome"]["interrupts"][0]
        tool = self._client_action(_events(self._resume(client, approval).text))
        response = self._resume(client, tool, payload={"title": "Page"})
        self.assertEqual(_events(response.text)[-1]["type"], "RUN_FINISHED")

    def test_parallel_approvals_require_complete_batch(self):
        driver = ParallelApprovalDriver()
        client = self._client(driver)
        interrupts = self._start(client)[-1]["outcome"]["interrupts"]
        self.assertEqual(len(interrupts), 2)
        self.assertEqual(self._resume(client, interrupts[0]).status_code, 409)
        events = _events(self._resume(client, *interrupts).text)
        self.assertEqual(events[-1]["type"], "RUN_FINISHED")
        self.assertNotIn("outcome", events[-1])
        self.assertEqual(driver.after_protected, 1)

    def test_client_tool_can_be_cancelled(self):
        client = self._client(ClientToolDriver())
        action = self._client_action(self._start(client))
        response = self._resume(client, action, status="cancelled")
        self.assertEqual(_events(response.text)[-1]["type"], "RUN_ERROR")
        self.assertIn("cancelled", response.text)

    def test_resumed_execution_obeys_timeout_and_records_failure(self):
        client = self._client(SlowApprovalDriver(), request_timeout=0.1)
        interrupt = self._start(client)[-1]["outcome"]["interrupts"][0]
        events = _events(self._resume(client, interrupt).text)
        self.assertEqual(events[-2]["type"], "TEXT_MESSAGE_END")
        self.assertEqual(events[-1], {"type": "RUN_ERROR", "message": "Response timed out"})
        status = client.get(f"/responses/{interrupt['metadata']['harnest']['callId']}", params={"sessionId": "thread"})
        self.assertEqual(status.json()["status"], "failed")

    def test_invalid_client_output_can_be_corrected_without_restarting(self):
        driver = TypedClientToolDriver()
        client = self._client(driver)
        action = self._client_action(self._start(client))
        for title, expected in [(42, "RUN_ERROR"), ("valid", "RUN_FINISHED")]:
            response = self._resume(client, action, payload={"title": title})
            self.assertEqual(_events(response.text)[-1]["type"], expected, response.text)
        self.assertEqual(len(driver.invocations), 1)

    def test_client_resume_refreshes_authenticated_credentials(self):
        driver = CredentialClientToolDriver()
        client = self._client(driver, authenticator=CredentialHeaderAuthenticator())
        events = self._start(client, headers={"x-test-user": "alice", "x-test-credential": "old"})
        for credential in ("fresh", "latest"):
            response = self._resume(client, self._client_action(events), payload={"title": "ok"},
                                    headers={"x-test-user": "alice", "x-test-credential": credential})
            events = _events(response.text)
        self.assertEqual(events[-1]["type"], "RUN_FINISHED")
        self.assertEqual(driver.seen, [("alice", "fresh"), ("alice", "fresh"), ("alice", "latest")])

    def test_external_wait_is_pollable_without_a_forged_user_resume(self):
        driver = ExternalLiveDriver(complete=False)
        client = self._client(driver)
        events = self._start(client)
        wait = next(e for e in events if e.get("name") == "harnest.external_wait")
        self.assertEqual(events[-1]["result"]["status"], "in_progress")
        status = client.get(f"/responses/{wait['value']['responseId']}", params={"sessionId": "thread"})
        self.assertEqual(status.json()["status"], "in_progress")
        self.assertEqual(driver.model_resumes, 0)


class SharedStreamLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_and_agui_share_capacity_and_disconnect_cleanup(self):
        """Either wire format must release the same concurrency gate on close."""

        for agui_first in (False, True):
            with self.subTest(agui_first=agui_first):
                await self._check_shared_capacity(agui_first)

    async def _check_shared_capacity(self, agui_first):
        """Queue one format behind the other, then close the active stream."""

        driver = CancellableLiveDriver()
        coordinator = InvocationCoordinator(
            driver=driver,
            approvals=InMemoryApprovalStore(),
            client_tools=InMemoryClientToolStore(),
            assets=MemoryAssetStore(),
            asset_stores={},
            semaphore=asyncio.Semaphore(1),
            request_timeout=5,
            max_request_bytes=1024,
        )
        streams = []
        requests = []
        for index in range(2):
            request = await coordinator.prepare_request(
                "hello",
                user_id="alice",
                session_id=None,
                metadata={},
                transport="test",
            )
            requests.append(request)
            encoder = (
                _AGUIEncoder(run_id=f"run-{index}").encode
                if (index == 0) == agui_first
                else None
            )
            streams.append(coordinator.stream_response(request, encoder=encoder))
            await anext(streams[-1])  # The created event precedes driver activation.
        await anext(streams[0])
        queued = asyncio.create_task(anext(streams[1]))
        await asyncio.sleep(0.02)
        self.assertFalse(queued.done())
        self.assertEqual(len(driver.invocations), 1)
        # ASGI disconnects cancel the task consuming the streaming response.
        active = asyncio.create_task(anext(streams[0]))
        await asyncio.sleep(0)
        active.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await active
        await streams[0].aclose()
        await asyncio.wait_for(queued, 1)
        async for _ in streams[1]:
            pass
        self.assertEqual(driver.cancelled, 1)
        self.assertEqual(len(driver.invocations), 2)
        statuses = [
            await coordinator.poll_json(
                response_id=r.invocation_id, user_id=r.user_id, session_id=r.session_id
            )
            for r in requests
        ]
        self.assertEqual([s["status"] for s in statuses], ["cancelled", "completed"])


class AGUIStateDeltaTests(AGUITestCase):
    def test_state_delta_is_rendered_as_a_json_patch_add(self):
        events = _events(self._run(self._client(StateDeltaDriver())).text)
        state_event = next(event for event in events if event["type"] == "STATE_DELTA")
        self.assertEqual(
            sorted(state_event["delta"], key=lambda op: op["path"]),
            [
                {"op": "add", "path": "/count", "value": 1},
                {"op": "add", "path": "/label", "value": "hi"},
            ],
        )

    def test_json_pointer_escape_handles_reserved_characters(self):
        self.assertEqual(_json_pointer_escape("a/b~c"), "a~1b~0c")


if __name__ == "__main__":
    unittest.main()
