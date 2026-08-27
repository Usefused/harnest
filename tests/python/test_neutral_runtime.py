import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any
import unittest

try:
    from fastapi.testclient import TestClient

    FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover - optional local dependency
    FASTAPI_AVAILABLE = False

from harnest.neutral_runtime import (
    AgentInfo,
    InvocationRequest,
    InvocationResult,
    NEUTRAL_USER_ID,
    RuntimeEvent,
    SessionConflictError,
    SessionRecord,
    create_neutral_app,
)


class FakeDriver:
    def __init__(self) -> None:
        self.info = AgentInfo(
            id="fake-agent",
            name="Fake Agent",
            description="A deterministic neutral runtime fixture.",
            card={"name": "Fake Agent", "description": "fixture"},
            framework="fake",
            mode="managed",
            extra_endpoints={"native": "/native"},
        )
        self.sessions: dict[tuple[str, str], SessionRecord] = {}
        self.invocations: list[InvocationRequest] = []
        self.closed = False
        self.fail_stream = False

    async def create_session(
        self,
        *,
        session_id: str,
        user_id: str,
        state: Mapping[str, Any],
    ) -> SessionRecord:
        key = (user_id, session_id)
        if key in self.sessions:
            raise SessionConflictError(session_id)
        session = SessionRecord(id=session_id, user_id=user_id, state=dict(state))
        self.sessions[key] = session
        return session

    async def get_session(
        self, *, session_id: str, user_id: str
    ) -> SessionRecord | None:
        return self.sessions.get((user_id, session_id))

    async def list_sessions(self, *, user_id: str) -> Sequence[SessionRecord]:
        return [
            session
            for (owner, _), session in self.sessions.items()
            if owner == user_id
        ]

    async def update_session(
        self,
        *,
        session_id: str,
        user_id: str,
        state_delta: Mapping[str, Any],
    ) -> SessionRecord | None:
        key = (user_id, session_id)
        current = self.sessions.get(key)
        if current is None:
            return None
        updated = SessionRecord(
            id=current.id,
            user_id=current.user_id,
            state={**dict(current.state), **dict(state_delta)},
        )
        self.sessions[key] = updated
        return updated

    async def delete_session(self, *, session_id: str, user_id: str) -> bool:
        return self.sessions.pop((user_id, session_id), None) is not None

    def events(self) -> list[RuntimeEvent]:
        return [
            {"type": "message", "role": "assistant", "text": "hel"},
            {"type": "message", "role": "assistant", "text": "lo"},
            {
                "type": "tool_call",
                "id": "call-1",
                "name": "echo",
                "arguments": {"text": "hello"},
            },
            {
                "type": "tool_result",
                "id": "call-1",
                "name": "echo",
                "result": "hello",
            },
            {"type": "graph_output", "output": 42},
        ]

    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        self.invocations.append(request)
        return InvocationResult(
            text="hello",
            events=self.events(),
            result=42,
            session_id=request.session_id,
            metadata=request.metadata,
        )

    async def stream(
        self, request: InvocationRequest
    ) -> AsyncIterator[RuntimeEvent]:
        self.invocations.append(request)
        for event in self.events():
            await asyncio.sleep(0)
            yield event
        if self.fail_stream:
            raise RuntimeError("driver failed")

    async def close(self) -> None:
        self.closed = True


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi is not installed")
class NeutralRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.driver = FakeDriver()
        self.client_context = TestClient(create_neutral_app(self.driver))
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.assertTrue(self.driver.closed)

    def test_discovery_and_session_crud_use_the_driver(self):
        self.assertEqual(self.client.get("/healthz").json(), {"status": "ok"})
        self.assertEqual(
            self.client.get("/.well-known/agent-card.json").json(),
            {"name": "Fake Agent", "description": "fixture"},
        )
        agent = self.client.get("/agent").json()
        self.assertEqual(agent["id"], "fake-agent")
        self.assertEqual(agent["framework"], "fake")
        self.assertEqual(agent["endpoints"]["native"], "/native")

        created = self.client.post(
            "/sessions", json={"id": "one", "state": {"count": 1}}
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json(), {"id": "one", "state": {"count": 1}})
        self.assertEqual(
            self.client.post("/sessions", json={"id": "one"}).status_code, 409
        )
        updated = self.client.patch(
            "/sessions/one", json={"stateDelta": {"ready": True}}
        )
        self.assertEqual(
            updated.json(), {"id": "one", "state": {"count": 1, "ready": True}}
        )
        self.assertEqual(self.client.get("/sessions").json(), {
            "sessions": [
                {"id": "one", "state": {"count": 1, "ready": True}}
            ]
        })
        self.assertEqual(self.client.delete("/sessions/one").status_code, 204)
        self.assertEqual(self.client.get("/sessions/one").status_code, 404)

    def test_json_response_preserves_the_public_envelope(self):
        session = self.client.post("/sessions", json={"id": "json"})
        self.assertEqual(session.status_code, 201)
        response = self.client.post(
            "/responses",
            json={
                "input": "hello",
                "sessionId": "json",
                "metadata": {"source": "test"},
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["id"].startswith("resp_"))
        self.assertNotIn("responseId", body)
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["sessionId"], "json")
        self.assertEqual(body["outputText"], "hello")
        self.assertEqual(body["result"], 42)
        self.assertEqual(
            body["output"][0],
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hello"}],
            },
        )
        self.assertEqual(body["output"][1]["type"], "tool_call")
        self.assertEqual(body["output"][2]["callId"], "call-1")
        self.assertEqual(body["output"][3], {"type": "output", "value": 42})
        request = self.driver.invocations[-1]
        self.assertEqual(request.user_id, NEUTRAL_USER_ID)
        self.assertEqual(request.metadata, {"source": "test"})

    def test_sse_owns_event_names_sequence_and_error_framing(self):
        self.client.post("/sessions", json={"id": "stream"})
        response = self.client.post(
            "/responses",
            json={"input": "hello", "sessionId": "stream", "stream": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: response.created", response.text)
        self.assertIn("event: response.text.delta", response.text)
        self.assertIn("event: response.tool_call", response.text)
        self.assertIn("event: response.tool_result", response.text)
        self.assertIn("event: response.completed", response.text)
        payloads = [
            __import__("json").loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(
            [payload["sequence"] for payload in payloads], [0, 1, 2, 3, 4, 5]
        )
        self.assertEqual(payloads[-1]["outputText"], "hello")
        self.assertEqual(payloads[-1]["result"], 42)

        self.driver.fail_stream = True
        failed = self.client.post(
            "/responses",
            json={"input": "hello", "sessionId": "stream", "stream": True},
        )
        self.assertIn("event: error", failed.text)
        self.assertIn('"error": "driver failed"', failed.text)

    def test_live_uses_the_same_sequence_and_output_contract(self):
        self.client.post("/sessions", json={"id": "live"})
        with self.client.websocket_connect("/live") as websocket:
            websocket.send_json({"type": "connect", "sessionId": "live"})
            self.assertEqual(
                websocket.receive_json(),
                {"type": "session.connected", "sessionId": "live"},
            )
            websocket.send_json(
                {
                    "type": "response.create",
                    "requestId": "request-1",
                    "input": "hello",
                    "metadata": {"source": "live"},
                }
            )
            frames = []
            while True:
                value = websocket.receive_json()
                frames.append(value)
                if value["type"] == "response.completed":
                    break
            websocket.send_json({"type": "session.close"})

        self.assertEqual([frame["sequence"] for frame in frames], [0, 1, 2, 3, 4, 5])
        self.assertTrue(all(frame["requestId"] == "request-1" for frame in frames))
        self.assertEqual(frames[-1]["outputText"], "hello")
        self.assertEqual(frames[-1]["metadata"], {"source": "live"})

    def test_validation_is_shared_by_all_drivers(self):
        self.assertEqual(
            self.client.post(
                "/responses", content="{}", headers={"content-type": "text/plain"}
            ).status_code,
            415,
        )
        self.assertEqual(self.client.post("/responses", json={}).status_code, 400)
        self.assertEqual(
            self.client.post(
                "/responses", json={"input": "hello", "unknown": True}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.patch("/sessions/missing", json={"stateDelta": {}}).status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
