import asyncio
import json
import unittest
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from harnest.neutral_runtime import (
    InvocationRequest,
    RuntimeEvent,
    create_neutral_app,
    create_neutral_router,
)
from harnest.runtime_agui import _AGUIEncoder, _json_pointer_escape
from harnest.agent.approval import InMemoryApprovalStore
from harnest.assets import MemoryAssetStore
from harnest.client_tool import InMemoryClientToolStore
from harnest.runtime_invocation import InvocationCoordinator

from test_neutral_runtime import (
    ApprovalDriver,
    CancellableLiveDriver,
    FakeDriver,
    HeaderAuthenticator,
)


class StateDeltaDriver(FakeDriver):
    """Emit one state-changing turn ahead of the fixture's usual events."""

    async def stream(self, request: InvocationRequest) -> AsyncIterator[RuntimeEvent]:
        self.invocations.append(request)
        yield {"type": "state_delta", "delta": {"count": 1, "label": "hi"}}
        yield {"type": "message", "role": "assistant", "text": "ok"}


def _events(response_text: str) -> list[dict[str, Any]]:
    """Parse an AG-UI SSE body into its ordered list of decoded events."""

    return [
        json.loads(line[len("data: ") :])
        for line in response_text.splitlines()
        if line.startswith("data: ")
    ]


class AGUIRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.driver = FakeDriver()
        self.client_context = TestClient(create_neutral_app(self.driver))
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)

    def _run(self, driver_client, *, thread_id="thread-1", text="hello"):
        # An explicit threadId is a session id and, like /responses, must
        # already exist; create it first rather than relying on auto-create.
        driver_client.post("/sessions", json={"id": thread_id})
        return driver_client.post(
            "/agui",
            json={
                "threadId": thread_id,
                "runId": "run-1",
                "messages": [{"role": "user", "content": text}],
            },
        )

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
        self.assertEqual(
            events[2],
            {
                "type": "TEXT_MESSAGE_CONTENT",
                "messageId": message_id,
                "delta": "hel",
            },
        )
        self.assertEqual(
            events[3],
            {
                "type": "TEXT_MESSAGE_CONTENT",
                "messageId": message_id,
                "delta": "lo",
            },
        )
        self.assertEqual(events[5]["toolCallName"], "echo")
        self.assertEqual(json.loads(events[6]["delta"]), {"text": "hello"})
        self.assertEqual(events[7]["toolCallId"], events[5]["toolCallId"])
        self.assertEqual(events[8]["content"], "hello")

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

    def test_agui_run_rejects_a_body_without_user_content(self):
        response = self.client.post(
            "/agui", json={"threadId": "thread-1", "messages": []}
        )
        self.assertEqual(response.status_code, 400)

    def test_agui_run_reports_run_error_for_required_human_approval(self):
        with TestClient(create_neutral_app(ApprovalDriver())) as client:
            response = self._run(client)
        self.assertEqual(response.status_code, 200)
        events = _events(response.text)
        self.assertEqual(events[-1]["type"], "RUN_ERROR")
        self.assertIn("approval", events[-1]["message"])

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
            response = client.post(
                "/agui",
                json={
                    "messages": [{"role": "user", "content": "hi"}],
                    "padding": "x" * 2000,
                },
            )
        self.assertEqual(response.status_code, 413)
        self.assertFalse(driver.sessions)
        self.assertFalse(driver.invocations)

    def test_adapter_preserves_authentication_and_session_ownership(self):
        """Alternate encoding must not create another session authorization path."""

        driver = FakeDriver()
        with TestClient(
            create_neutral_app(driver, authenticator=HeaderAuthenticator())
        ) as client:
            payload = {
                "threadId": "private",
                "messages": [{"role": "user", "content": "hi"}],
            }
            client.post(
                "/sessions", headers={"x-test-user": "alice"}, json={"id": "private"}
            )
            self.assertEqual(client.post("/agui", json=payload).status_code, 401)
            self.assertEqual(
                client.post(
                    "/agui", headers={"x-test-user": "bob"}, json=payload
                ).status_code,
                404,
            )
            response = client.post(
                "/agui", headers={"x-test-user": "alice"}, json=payload
            )
            self.assertEqual(_events(response.text)[-1]["type"], "RUN_FINISHED")
            self.assertEqual(driver.invocations[-1].user_id, "alice")

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


class AGUIStateDeltaTests(unittest.TestCase):
    def test_state_delta_is_rendered_as_a_json_patch_add(self):
        with TestClient(create_neutral_app(StateDeltaDriver())) as client:
            client.post("/sessions", json={"id": "thread-1"})
            response = client.post(
                "/agui",
                json={
                    "threadId": "thread-1",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
        events = _events(response.text)
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
