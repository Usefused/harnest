import asyncio
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any
import unittest

try:
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

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
from harnest.runtime_auth import (
    AuthPrincipal,
    AuthenticationError,
)
from harnest.approval import require_human_approval
from harnest.tool import tool


@tool
@require_human_approval(message="Approve sending {value}?")
def _protected_send(value: str) -> str:
    """Send a protected value."""

    return f"sent:{value}"


class HeaderAuthenticator:
    async def authenticate(self, connection):
        user_id = connection.headers.get("x-test-user")
        if not user_id:
            raise AuthenticationError()
        return AuthPrincipal(user_id, {"source": "test"})


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
        self.empty_output = False

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
        if self.empty_output:
            return []
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
            text="" if self.empty_output else "hello",
            events=self.events(),
            result=None if self.empty_output else 42,
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


class ApprovalDriver(FakeDriver):
    def __init__(self) -> None:
        super().__init__()
        self.before_protected = 0
        self.after_protected = 0

    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        self.before_protected += 1
        value = await _protected_send(request.input)
        self.after_protected += 1
        return InvocationResult(
            text=value,
            events=({"type": "message", "role": "assistant", "text": value},),
            result=None,
            session_id=request.session_id,
            metadata=request.metadata,
        )

    async def stream(self, request: InvocationRequest) -> AsyncIterator[RuntimeEvent]:
        result = await self.invoke(request)
        for event in result.events:
            yield dict(event)


class MultipleApprovalDriver(ApprovalDriver):
    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        self.before_protected += 1
        first = await _protected_send(f"{request.input}:one")
        second = await _protected_send(f"{request.input}:two")
        self.after_protected += 1
        text = f"{first},{second}"
        return InvocationResult(
            text=text,
            events=({"type": "message", "role": "assistant", "text": text},),
            result=None,
            session_id=request.session_id,
            metadata=request.metadata,
        )


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

    def test_development_playground_is_bundled_and_framework_neutral(self):
        page = self.client.get("/")
        stylesheet = self.client.get("/_harnest/playground.css")
        javascript = self.client.get("/_harnest/playground.js")

        self.assertEqual(page.status_code, 200)
        self.assertIn("Harnest Playground", page.text)
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertEqual(stylesheet.headers["cache-control"], "no-cache")
        self.assertEqual(javascript.headers["cache-control"], "no-cache")
        self.assertIn("default-src 'self'", page.headers["content-security-policy"])
        self.assertEqual(stylesheet.status_code, 200)
        self.assertIn("text/css", stylesheet.headers["content-type"])
        self.assertEqual(javascript.status_code, 200)
        self.assertIn("text/javascript", javascript.headers["content-type"])
        for endpoint in ('"/agent"', '"/sessions"', '"/responses"', '"/live"'):
            self.assertIn(endpoint, javascript.text)
        self.assertIn("/approvals/", javascript.text)
        self.assertIn("Human approval required", javascript.text)
        self.assertIn('id="session-state-empty"', page.text)
        self.assertIn('id="session-menu"', page.text)
        self.assertIn('id="trace-timeline"', page.text)
        self.assertIn('id="trace-tab"', page.text)
        self.assertIn('aria-haspopup="listbox"', page.text)
        self.assertIn('data-active="stream"', page.text)
        self.assertLess(
            page.text.index('id="session-select"'),
            page.text.index('<main class="workspace">'),
        )
        self.assertIn("height: 100dvh", stylesheet.text)
        self.assertIn("overscroll-behavior: contain", stylesheet.text)
        self.assertIn("typing-bubble", stylesheet.text)
        self.assertIn(".trace-entry", stylesheet.text)
        self.assertIn('.tool-event[data-status="completed"]', stylesheet.text)
        self.assertIn(".session-option[aria-selected=\"true\"]", stylesheet.text)
        self.assertIn("renderSessionState(session.state || {})", javascript.text)
        self.assertIn("ui.sessionState.hidden = false", javascript.text)
        self.assertIn("syncSessionPicker()", javascript.text)
        self.assertIn('traces: "/_harnest/traces"', javascript.text)
        self.assertIn("appendToolResult", javascript.text)
        self.assertIn('.dataset.active = runtime.transport', javascript.text)
        self.assertIn("showTypingIndicator()", javascript.text)
        for native_endpoint in ('"/run"', '"/run_sse"', '"/run_live"'):
            self.assertNotIn(native_endpoint, javascript.text)
        self.assertNotIn("localStorage", javascript.text)
        self.assertNotIn("sessionStorage", javascript.text)

    def test_playground_renders_nested_application_state_updates(self):
        self.client.post(
            "/sessions",
            json={
                "id": "state-render",
                "state": {"workflow": {"stage": "new"}},
            },
        )
        state_delta = {
            "customer": {
                "id": "cus_demo_123",
                "tier": "enterprise",
                "preferences": {
                    "locale": "en-GB",
                    "notifications": True,
                },
            },
            "workflow": {
                "stage": "triaged",
                "tags": ["api", "urgent"],
            },
            "counters": {"messages": 3, "toolCalls": 1},
        }

        updated = self.client.patch(
            "/sessions/state-render",
            json={"stateDelta": state_delta},
        )

        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["state"], state_delta)
        self.assertEqual(
            self.client.get("/sessions/state-render").json()["state"],
            state_delta,
        )
        javascript = self.client.get("/_harnest/playground.js").text
        self.assertIn("renderSessionState(session.state || {})", javascript)
        self.assertIn(
            "ui.sessionState.textContent = pretty(state || {})",
            javascript,
        )
        self.assertIn("ui.sessionStateEmpty.hidden = !empty", javascript)

    def test_server_policy_controls_request_limit_and_playground(self):
        app = create_neutral_app(
            FakeDriver(),
            max_request_bytes=1024,
            playground_enabled=False,
        )
        with TestClient(app) as client:
            self.assertEqual(client.get("/").status_code, 404)
            self.assertEqual(client.get("/_harnest/traces").status_code, 404)
            response = client.post(
                "/sessions",
                json={"id": "large", "state": {"value": "x" * 1024}},
            )
            self.assertEqual(response.status_code, 413)
            self.assertEqual(
                response.json()["detail"],
                "Request body exceeds 1KiB",
            )

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

        traces = self.client.get(
            "/_harnest/traces", params={"sessionId": "json"}
        ).json()["traces"]
        self.assertEqual(traces[0]["id"], body["id"])
        self.assertEqual(traces[0]["status"], "completed")
        self.assertEqual(traces[0]["transport"], "response")
        self.assertIn(
            "tool", {entry["category"] for entry in traces[0]["entries"]}
        )
        detail = self.client.get(f"/_harnest/traces/{body['id']}")
        self.assertEqual(detail.status_code, 200)
        self.assertNotIn("userId", detail.json())

    def test_playground_trace_captures_structured_agent_logs(self):
        class LoggingDriver(FakeDriver):
            async def invoke(self, request):
                logging.getLogger("harnest.agent.test").warning(
                    "catalog.lookup.completed",
                    extra={"result_count": 3},
                )
                return await super().invoke(request)

        driver = LoggingDriver()
        with TestClient(create_neutral_app(driver)) as client:
            client.post("/sessions", json={"id": "logged"})
            response = client.post(
                "/responses",
                json={"input": "hello", "sessionId": "logged"},
            )
            trace = client.get(
                f"/_harnest/traces/{response.json()['id']}"
            ).json()
        logs = [entry for entry in trace["entries"] if entry["category"] == "log"]
        self.assertEqual(logs[0]["message"], "catalog.lookup.completed")
        self.assertEqual(logs[0]["detail"]["result_count"], 3)

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
        stream_traces = self.client.get(
            "/_harnest/traces", params={"sessionId": "stream"}
        ).json()["traces"]
        self.assertEqual(
            [trace["status"] for trace in stream_traces[:2]],
            ["failed", "completed"],
        )
        self.assertTrue(
            all(trace["transport"] == "stream" for trace in stream_traces[:2])
        )

    def test_reasoning_only_completion_is_never_reported_as_success(self):
        self.driver.empty_output = True
        self.client.post("/sessions", json={"id": "empty"})

        response = self.client.post(
            "/responses", json={"input": "hello", "sessionId": "empty"}
        )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["detail"],
            "Agent completed without customer-facing output",
        )

        stream = self.client.post(
            "/responses",
            json={"input": "hello", "sessionId": "empty", "stream": True},
        )
        self.assertIn("event: error", stream.text)
        self.assertNotIn("event: response.completed", stream.text)

        with self.client.websocket_connect("/live") as websocket:
            websocket.send_json({"type": "connect", "sessionId": "empty"})
            websocket.receive_json()
            websocket.send_json({"type": "response.create", "input": "hello"})
            self.assertEqual(websocket.receive_json()["type"], "response.created")
            failure = websocket.receive_json()

        self.assertEqual(failure["type"], "error")
        self.assertEqual(
            failure["error"], "Agent completed without customer-facing output"
        )

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
        live_trace = self.client.get(
            "/_harnest/traces", params={"sessionId": "live"}
        ).json()["traces"][0]
        self.assertEqual(live_trace["status"], "completed")
        self.assertEqual(live_trace["transport"], "live")

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

    def test_injected_authentication_scopes_sessions_and_invocations(self):
        driver = FakeDriver()
        app = create_neutral_app(driver, authenticator=HeaderAuthenticator())
        with TestClient(app) as client:
            self.assertEqual(client.get("/").status_code, 200)
            self.assertEqual(
                client.get("/_harnest/playground.js").status_code,
                200,
            )
            self.assertEqual(client.get("/agent").status_code, 200)
            self.assertEqual(client.get("/sessions").status_code, 401)
            self.assertEqual(client.get("/_harnest/traces").status_code, 401)
            self.assertEqual(
                client.post(
                    "/responses",
                    json={"input": "protected"},
                ).status_code,
                401,
            )
            with self.assertRaises(WebSocketDisconnect) as rejected:
                with client.websocket_connect("/live"):
                    pass
            self.assertEqual(rejected.exception.code, 4401)
            alice = {"x-test-user": "alice"}
            bob = {"x-test-user": "bob"}
            created = client.post(
                "/sessions",
                headers=alice,
                json={"id": "shared", "state": {"owner": "alice"}},
            )
            self.assertEqual(created.status_code, 201)
            self.assertEqual(
                client.get("/sessions/shared", headers=bob).status_code,
                404,
            )
            response = client.post(
                "/responses",
                headers=alice,
                json={"input": "hello", "sessionId": "shared"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(driver.invocations[-1].user_id, "alice")
            alice_traces = client.get(
                "/_harnest/traces", headers=alice
            ).json()["traces"]
            self.assertEqual(len(alice_traces), 1)
            self.assertEqual(
                client.get(
                    f"/_harnest/traces/{alice_traces[0]['id']}",
                    headers=bob,
                ).status_code,
                404,
            )
            with client.websocket_connect(
                "/live", headers=alice
            ) as websocket:
                websocket.send_json({"type": "connect", "sessionId": "shared"})
                self.assertEqual(
                    websocket.receive_json()["type"], "session.connected"
                )
                websocket.send_json({"type": "session.close"})


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi is not installed")
class ApprovalTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = TestClient(create_neutral_app(ApprovalDriver()))
        self.client = self.context.__enter__()
        self.client.post("/sessions", json={"id": "approval-session"})

    def tearDown(self) -> None:
        self.context.__exit__(None, None, None)

    def test_json_approval_resumes_once_and_denial_does_not_execute(self):
        response = self.client.post(
            "/responses",
            json={"input": "record", "sessionId": "approval-session"},
        )
        self.assertEqual(response.status_code, 200)
        required = response.json()
        self.assertEqual(required["status"], "requires_action")
        self.assertEqual(required["requiredAction"]["type"], "human_approval")
        approval_id = required["requiredAction"]["id"]

        resumed = self.client.post(
            f"/approvals/{approval_id}", json={"decision": "approve"}
        )
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.json()["status"], "completed")
        self.assertEqual(resumed.json()["outputText"], "sent:record")
        self.assertEqual(
            self.client.post(
                f"/approvals/{approval_id}", json={"decision": "approve"}
            ).status_code,
            409,
        )

        denied_request = self.client.post(
            "/responses",
            json={"input": "deny", "sessionId": "approval-session"},
        ).json()
        denied = self.client.post(
            f"/approvals/{denied_request['requiredAction']['id']}",
            json={"decision": "deny"},
        )
        self.assertEqual(denied.json()["status"], "denied")

    def test_approval_continues_exact_task_without_replaying_prior_work(self):
        driver = ApprovalDriver()
        app = create_neutral_app(driver)
        with TestClient(app) as client:
            client.post("/sessions", json={"id": "s"})
            required = client.post(
                "/responses", json={"input": "once", "sessionId": "s"}
            ).json()
            self.assertEqual(driver.before_protected, 1)
            self.assertEqual(driver.after_protected, 0)
            completed = client.post(
                f"/approvals/{required['requiredAction']['id']}",
                json={"decision": "approve"},
            )
            self.assertEqual(completed.status_code, 200)
            self.assertEqual(driver.before_protected, 1)
            self.assertEqual(driver.after_protected, 1)

    def test_multiple_sequential_approvals_resume_same_task(self):
        driver = MultipleApprovalDriver()
        app = create_neutral_app(driver)
        with TestClient(app) as client:
            client.post("/sessions", json={"id": "s"})
            first = client.post(
                "/responses", json={"input": "sequence", "sessionId": "s"}
            ).json()
            second = client.post(
                f"/approvals/{first['requiredAction']['id']}",
                json={"decision": "approve"},
            ).json()
            self.assertEqual(second["status"], "requires_action")
            completed = client.post(
                f"/approvals/{second['requiredAction']['id']}",
                json={"decision": "approve"},
            ).json()
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(
                completed["outputText"], "sent:sequence:one,sent:sequence:two"
            )
            self.assertEqual(driver.before_protected, 1)
            self.assertEqual(driver.after_protected, 1)

    def test_sse_and_live_emit_approval_requested(self):
        response = self.client.post(
            "/responses",
            json={
                "input": "stream",
                "sessionId": "approval-session",
                "stream": True,
            },
        )
        self.assertIn("event: approval.requested", response.text)
        self.assertIn('"status": "requires_action"', response.text)

        with self.client.websocket_connect("/live") as websocket:
            websocket.send_json(
                {"type": "connect", "sessionId": "approval-session"}
            )
            websocket.receive_json()
            websocket.send_json({"type": "response.create", "input": "live"})
            self.assertEqual(websocket.receive_json()["type"], "response.created")
            self.assertEqual(websocket.receive_json()["type"], "approval.requested")
            self.assertEqual(
                websocket.receive_json()["status"], "requires_action"
            )
            websocket.send_json({"type": "session.close"})

    def test_approval_decision_is_scoped_to_authenticated_principal(self):
        app = create_neutral_app(
            ApprovalDriver(), authenticator=HeaderAuthenticator()
        )
        with TestClient(app) as client:
            alice = {"x-test-user": "alice"}
            bob = {"x-test-user": "bob"}
            client.post("/sessions", headers=alice, json={"id": "alice-session"})
            required = client.post(
                "/responses",
                headers=alice,
                json={"input": "private", "sessionId": "alice-session"},
            ).json()
            endpoint = f"/approvals/{required['requiredAction']['id']}"
            self.assertEqual(
                client.post(
                    endpoint, headers=bob, json={"decision": "approve"}
                ).status_code,
                404,
            )
            self.assertEqual(
                client.post(
                    endpoint, headers=alice, json={"decision": "approve"}
                ).status_code,
                200,
            )


if __name__ == "__main__":
    unittest.main()
