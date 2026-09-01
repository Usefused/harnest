import asyncio
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Annotated, Any
import unittest
from dataclasses import replace

from pydantic import BaseModel, ConfigDict

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
    SessionMessage,
    SessionRecord,
    create_neutral_app,
)
from harnest.assets import MemoryAssetStore
from harnest.content import Image, ImageConstraints
from harnest.runtime_auth import (
    AuthPrincipal,
    AuthenticationError,
)
from harnest.approval import request_human_approval, require_human_approval
from harnest.client_tool import client_tool
from harnest.tool import tool


@tool
@require_human_approval(message="Approve sending {value}?")
def _protected_send(value: str) -> str:
    """Send a protected value."""

    return f"sent:{value}"


@client_tool
def _browser_open(url: str) -> dict[str, str]:
    """Open a URL in the caller's browser and return the page title."""

    raise AssertionError("client tool declaration bodies must not run")


@client_tool
@require_human_approval(message="Approve opening {url} in the client?")
def _protected_browser_open(url: str) -> dict[str, str]:
    """Open an approved URL in the caller's browser."""

    raise AssertionError("client tool declaration bodies must not run")


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
        self.session_messages: dict[tuple[str, str], list[SessionMessage]] = {}
        self.invocations: list[InvocationRequest] = []
        self.session_list_requests: list[tuple[str, str | None, int | None]] = []
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
        self.session_messages[key] = []
        return session

    async def get_session(
        self, *, session_id: str, user_id: str
    ) -> SessionRecord | None:
        return self.sessions.get((user_id, session_id))

    async def list_sessions(
        self,
        *,
        user_id: str,
        after: str | None = None,
        limit: int | None = None,
    ) -> Sequence[SessionRecord]:
        self.session_list_requests.append((user_id, after, limit))
        sessions = sorted(
            (
                session
                for (owner, _), session in self.sessions.items()
                if owner == user_id and (after is None or session.id > after)
            ),
            key=lambda item: item.id,
        )
        return sessions if limit is None else sessions[:limit]

    async def get_session_messages(
        self, *, session_id: str, user_id: str
    ) -> Sequence[SessionMessage] | None:
        key = (user_id, session_id)
        if key not in self.sessions:
            return None
        return tuple(self.session_messages[key])

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
        key = (user_id, session_id)
        self.session_messages.pop(key, None)
        return self.sessions.pop(key, None) is not None

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
        messages = self.session_messages[(request.user_id, request.session_id)]
        messages.extend(
            (
                SessionMessage(
                    id=f"{request.invocation_id}:user",
                    role="user",
                    content=request.input,
                    metadata={"fake": {"kind": "input"}},
                ),
                SessionMessage(
                    id=f"{request.invocation_id}:assistant",
                    role="assistant",
                    content="hello",
                    metadata={"fake": {"kind": "output"}},
                ),
            )
        )
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


class StructuredInput(BaseModel):
    model_config = ConfigDict(strict=True)

    query: str
    limit: int


class StructuredInputDriver(FakeDriver):
    def __init__(self) -> None:
        super().__init__()
        self.info = replace(self.info, input_schema=StructuredInput)


class MediaInput(BaseModel):
    model_config = ConfigDict(strict=True)

    prompt: str
    image: Annotated[
        Image,
        ImageConstraints(
            media_types=frozenset({"image/png"}),
            max_width=4,
            max_height=4,
        ),
    ]


class MediaInputDriver(FakeDriver):
    def __init__(self) -> None:
        super().__init__()
        self.info = replace(self.info, input_schema=MediaInput)


def _png(width: int, height: int) -> bytes:
    """Build the parser fixture without adding an imaging dependency."""

    ihdr = (
        width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
    )

    def chunk(kind: bytes, data: bytes) -> bytes:
        return len(data).to_bytes(4, "big") + kind + data + b"\x00\x00\x00\x00"

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


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


class DynamicApprovalDriver(FakeDriver):
    def __init__(self) -> None:
        super().__init__()
        self.evaluations = 0
        self.executions = 0

    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        self.evaluations += 1
        if request.input == "safe":
            text = "skipped:low-risk"
        else:
            async with request_human_approval(
                action="typescript.execute",
                message="Execute TypeScript with network access?",
                arguments={
                    "capabilities": ["network"],
                    "sourceHash": "sha256:fixture",
                },
            ):
                self.executions += 1
                text = "executed:typescript"
        return InvocationResult(
            text=text,
            events=({"type": "message", "role": "assistant", "text": text},),
            result=None,
            session_id=request.session_id,
            metadata=request.metadata,
        )

    async def stream(self, request: InvocationRequest) -> AsyncIterator[RuntimeEvent]:
        result = await self.invoke(request)
        for event in result.events:
            yield dict(event)


class ClientToolDriver(FakeDriver):
    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        result = await _browser_open(request.input)
        text = f"opened:{result['title']}"
        return InvocationResult(
            text=text,
            events=({"type": "message", "role": "assistant", "text": text},),
            result=result,
            session_id=request.session_id,
            metadata=request.metadata,
        )

    async def stream(self, request: InvocationRequest) -> AsyncIterator[RuntimeEvent]:
        result = await self.invoke(request)
        for event in result.events:
            yield dict(event)


class ApprovedClientToolDriver(FakeDriver):
    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        result = await _protected_browser_open(request.input)
        text = f"approved-opened:{result['title']}"
        return InvocationResult(
            text=text,
            events=({"type": "message", "role": "assistant", "text": text},),
            result=result,
            session_id=request.session_id,
            metadata=request.metadata,
        )

    async def stream(self, request: InvocationRequest) -> AsyncIterator[RuntimeEvent]:
        result = await self.invoke(request)
        for event in result.events:
            yield dict(event)


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
        self.assertEqual(
            agent["endpoints"]["sessionMessages"],
            "/sessions/{sessionId}/messages",
        )

        created = self.client.post(
            "/sessions", json={"id": "one", "state": {"count": 1}}
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(
            created.json(),
            {
                "id": "one",
                "userId": NEUTRAL_USER_ID,
                "state": {"count": 1},
                "applicationData": {},
                "createdAt": None,
                "updatedAt": None,
                "metadata": {},
            },
        )
        self.assertEqual(
            self.client.post("/sessions", json={"id": "one"}).status_code, 409
        )
        updated = self.client.patch(
            "/sessions/one", json={"stateDelta": {"ready": True}}
        )
        self.assertEqual(
            updated.json(),
            {
                "id": "one",
                "userId": NEUTRAL_USER_ID,
                "state": {"count": 1, "ready": True},
                "applicationData": {},
                "createdAt": None,
                "updatedAt": None,
                "metadata": {},
            },
        )
        self.assertEqual(self.client.get("/sessions").json(), {
            "sessions": [
                {
                    "id": "one",
                    "userId": NEUTRAL_USER_ID,
                    "state": {"count": 1, "ready": True},
                    "applicationData": {},
                    "createdAt": None,
                    "updatedAt": None,
                    "metadata": {},
                }
            ],
            "nextCursor": None,
        })
        self.assertEqual(self.client.delete("/sessions/one").status_code, 204)
        self.assertEqual(self.client.get("/sessions/one").status_code, 404)

    def test_assets_are_session_scoped_streamable_and_deletable(self):
        self.client.post("/sessions", json={"id": "assets"})
        self.client.post("/sessions", json={"id": "other"})
        payload = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"

        uploaded = self.client.post(
            "/sessions/assets/assets",
            content=payload,
            headers={"Content-Type": "application/pdf"},
        )

        self.assertEqual(uploaded.status_code, 201)
        asset_id = uploaded.json()["assetId"]
        path = f"/sessions/assets/assets/{asset_id}"
        self.assertEqual(
            self.client.head(path).headers["content-length"], str(len(payload))
        )
        self.assertEqual(self.client.get(path).content, payload)
        selected = self.client.get(path, headers={"Range": "bytes=5-9"})
        self.assertEqual(selected.status_code, 206)
        self.assertEqual(selected.content, payload[5:10])
        self.assertEqual(
            self.client.get(f"/sessions/other/assets/{asset_id}").status_code,
            404,
        )
        self.assertEqual(self.client.delete(path).status_code, 204)
        self.assertEqual(self.client.get(path).status_code, 404)

    def test_annotated_media_input_uses_inspected_asset_metadata(self):
        driver = MediaInputDriver()
        store = MemoryAssetStore(max_asset_bytes=1024, max_total_bytes=4096)
        with TestClient(create_neutral_app(driver, asset_store=store)) as client:
            client.post("/sessions", json={"id": "media"})
            uploaded = client.post(
                "/sessions/media/assets",
                content=_png(3, 4),
                headers={"Content-Type": "image/png"},
            )
            response = client.post(
                "/responses",
                json={
                    "sessionId": "media",
                    "input": {
                        "prompt": "inspect",
                        "image": {
                            "assetId": uploaded.json()["assetId"],
                            "width": 999,
                            "height": 999,
                        },
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        image = driver.invocations[-1].input["image"]
        self.assertEqual((image["width"], image["height"]), (3, 4))
        self.assertEqual(image["mediaType"], "image/png")

    def test_session_listing_supports_optional_keyset_pagination(self):
        for session_id in ("delta", "bravo", "alpha", "charlie"):
            response = self.client.post("/sessions", json={"id": session_id})
            self.assertEqual(response.status_code, 201)

        complete = self.client.get("/sessions")
        self.assertIsNone(complete.json()["nextCursor"])
        self.assertEqual(
            [item["id"] for item in complete.json()["sessions"]],
            ["alpha", "bravo", "charlie", "delta"],
        )
        first = self.client.get("/sessions", params={"limit": 2})
        self.assertEqual(
            [item["id"] for item in first.json()["sessions"]],
            ["alpha", "bravo"],
        )
        cursor = first.json()["nextCursor"]
        self.assertIsInstance(cursor, str)
        self.assertEqual(
            self.driver.session_list_requests[-1],
            (NEUTRAL_USER_ID, None, 3),
        )
        final = self.client.get("/sessions", params={"cursor": cursor})
        self.assertEqual(
            [item["id"] for item in final.json()["sessions"]],
            ["charlie", "delta"],
        )
        self.assertIsNone(final.json()["nextCursor"])
        self.assertEqual(
            self.driver.session_list_requests[-1],
            (NEUTRAL_USER_ID, "bravo", 101),
        )
        self.assertEqual(
            self.client.get("/sessions", params={"cursor": "invalid"}).status_code,
            400,
        )
        self.assertEqual(
            self.client.get("/sessions", params={"limit": 101}).status_code,
            422,
        )

    def test_default_session_page_is_bounded(self):
        for index in range(102):
            session_id = f"session-{index:03d}"
            key = (NEUTRAL_USER_ID, session_id)
            self.driver.sessions[key] = SessionRecord(
                id=session_id, user_id=NEUTRAL_USER_ID, state={}
            )
            self.driver.session_messages[key] = []

        first = self.client.get("/sessions").json()
        self.assertEqual(len(first["sessions"]), 100)
        self.assertEqual(first["sessions"][-1]["id"], "session-099")
        self.assertIsInstance(first["nextCursor"], str)
        final = self.client.get(
            "/sessions", params={"cursor": first["nextCursor"]}
        ).json()
        self.assertEqual(
            [item["id"] for item in final["sessions"]],
            ["session-100", "session-101"],
        )
        self.assertIsNone(final["nextCursor"])

    def test_development_playground_is_bundled_and_framework_neutral(self):
        page = self.client.get("/")
        stylesheet = self.client.get("/_harnest/playground.css")
        javascript = self.client.get("/_harnest/playground.js")

        self.assertEqual(page.status_code, 200)
        self.assertIn("Harnest Playground", page.text)
        self.assertIn('<span class="eyebrow">Playground</span>', page.text)
        self.assertNotIn("Development playground", page.text)
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
        self.assertIn(
            'approvalButton("Approve", "approve", "approval-button approval-button-approve")',
            javascript.text,
        )
        self.assertIn(
            'approvalButton("Deny", "deny", "approval-button approval-button-deny")',
            javascript.text,
        )
        self.assertIn("function approvalIsUnavailable(error)", javascript.text)
        self.assertIn("function markApprovalResolved(panel, decision)", javascript.text)
        self.assertIn("You approved this workflow step", javascript.text)
        self.assertIn("You denied this workflow step", javascript.text)
        self.assertIn("function startAgentResume(message)", javascript.text)
        self.assertIn('startAgentResume("Resuming agent after approval…")', javascript.text)
        self.assertIn("function renderRequiredAction(action, transport)", javascript.text)
        self.assertIn('action?.type === "human_approval"', javascript.text)
        self.assertIn('action?.type === "client_tool"', javascript.text)
        self.assertIn("/client-tools/", javascript.text)
        self.assertIn("Client tool · awaiting host result", javascript.text)
        self.assertIn(
            "This approval is no longer pending. Send the request again.",
            javascript.text,
        )
        self.assertIn('id="session-state-empty"', page.text)
        self.assertIn('id="session-menu"', page.text)
        self.assertIn('id="trace-timeline"', page.text)
        self.assertIn('id="trace-tab"', page.text)
        self.assertIn('id="logs-tab"', page.text)
        self.assertIn('id="logs-view"', page.text)
        self.assertIn('id="log-search"', page.text)
        self.assertIn('data-level="warning"', page.text)
        self.assertIn('aria-haspopup="listbox"', page.text)
        self.assertIn('data-active="stream"', page.text)
        self.assertIn('id="theme-trigger"', page.text)
        self.assertIn('id="theme-menu"', page.text)
        self.assertIn('data-theme-option="system"', page.text)
        self.assertIn('role="menuitemradio"', page.text)
        self.assertIn('data-theme-icon="system"', page.text)
        self.assertLess(
            page.text.index('id="session-select"'),
            page.text.index('<main class="workspace">'),
        )
        self.assertIn("height: 100dvh", stylesheet.text)
        self.assertIn("overscroll-behavior: contain", stylesheet.text)
        self.assertIn("typing-bubble", stylesheet.text)
        self.assertIn("typing-label", stylesheet.text)
        self.assertIn("typing-glow", stylesheet.text)
        self.assertIn(".send-button { position: absolute", stylesheet.text)
        self.assertIn(".approval-button-approve", stylesheet.text)
        self.assertIn('.approval-event[data-status="unavailable"]', stylesheet.text)
        self.assertIn(".approval-note", stylesheet.text)
        self.assertIn(".approval-resolution", stylesheet.text)
        self.assertIn(".approval-resolution-marker", stylesheet.text)
        self.assertIn(".client-tool-action", stylesheet.text)
        self.assertIn(".client-tool-result", stylesheet.text)
        self.assertIn(".composer textarea::placeholder", stylesheet.text)
        self.assertIn("font-size: 14px; font-weight: 400", stylesheet.text)
        self.assertIn(':root[data-theme="light"]', stylesheet.text)
        self.assertIn(".trace-entry", stylesheet.text)
        self.assertIn(".log-entry", stylesheet.text)
        self.assertIn(".log-level.active", stylesheet.text)
        self.assertIn('.tool-event[data-status="completed"]', stylesheet.text)
        self.assertIn(".session-option[aria-selected=\"true\"]", stylesheet.text)
        self.assertIn("renderSessionState(session.state || {})", javascript.text)
        self.assertIn("ui.sessionState.hidden = false", javascript.text)
        self.assertIn("syncSessionPicker()", javascript.text)
        self.assertIn('traces: "/_harnest/traces"', javascript.text)
        self.assertIn("function renderLogs()", javascript.text)
        self.assertIn('"harnest.playground.theme"', javascript.text)
        self.assertIn("function selectTheme(theme, persist = true)", javascript.text)
        self.assertIn("function toggleThemeMenu(force)", javascript.text)
        self.assertIn('entry.category !== "log"', javascript.text)
        self.assertIn('selectInspectorView("logs")', javascript.text)
        self.assertIn('logging ? "Logs" : tracing ? "Trace" : "State"', javascript.text)
        self.assertIn("appendToolResult", javascript.text)
        self.assertIn("function attachPendingTools(turn)", javascript.text)
        self.assertIn("function bindToolAccordion(detail, list)", javascript.text)
        self.assertIn("function createToolTrayCaret()", javascript.text)
        self.assertIn("function updateToolTraySummary(tray)", javascript.text)
        self.assertIn("Used ${tools.length} tools", javascript.text)
        self.assertIn('aria-label", "Tools called by the agent', javascript.text)
        self.assertIn(".turn-tools", stylesheet.text)
        self.assertIn(".turn-tool-list", stylesheet.text)
        self.assertIn(".turn-tools[open]", stylesheet.text)
        self.assertIn(".turn-tools > summary:focus-visible", stylesheet.text)
        self.assertIn('.dataset.active = runtime.transport', javascript.text)
        self.assertIn("showTypingIndicator()", javascript.text)
        self.assertIn('label.textContent = "Thinking"', javascript.text)
        self.assertIn(
            "if (runtime.streamingBubble) {\n    clearTypingIndicator();",
            javascript.text,
        )
        self.assertIn(
            "frame.outputText && !runtime.responseAssistantTurn",
            javascript.text,
        )
        for native_endpoint in ('"/run"', '"/run_sse"', '"/run_live"'):
            self.assertNotIn(native_endpoint, javascript.text)
        self.assertEqual(javascript.text.count("window.localStorage"), 2)
        self.assertNotIn('localStorage.setItem("session', javascript.text)
        self.assertNotIn('localStorage.setItem("bearer', javascript.text)
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

    def test_playground_new_session_resets_conversation_owned_state(self):
        javascript = self.client.get("/_harnest/playground.js").text

        self.assertIn("function resetConversation()", javascript)
        self.assertIn(
            "ui.conversation.replaceChildren(ui.emptyState)",
            javascript,
        )
        self.assertIn("runtime.toolCards.clear()", javascript)
        self.assertIn("runtime.clientToolCards.clear()", javascript)
        self.assertIn(
            "setActiveSession(session.id, clearConversation)",
            javascript,
        )
        self.assertIn(
            "return runtime.sessionId || createSession(false)",
            javascript,
        )

    def test_playground_marks_unfinished_tools_failed_on_request_error(self):
        javascript = self.client.get("/_harnest/playground.js").text
        stylesheet = self.client.get("/_harnest/playground.css").text

        self.assertIn("detail.open = false", javascript)
        self.assertNotIn("detail.open = true", javascript)
        self.assertIn("function failRunningTools()", javascript)
        self.assertIn(
            'if (card.detail.dataset.status !== "running") continue',
            javascript,
        )
        self.assertIn('card.detail.dataset.status = "failed"', javascript)
        self.assertIn('card.status.textContent = "Failed"', javascript)
        self.assertIn("failRunningTools();", javascript)
        self.assertIn('.tool-event[data-status="failed"]', stylesheet)

    def test_playground_preserves_text_and_tool_call_timeline(self):
        javascript = self.client.get("/_harnest/playground.js").text

        self.assertIn("function beginToolBoundary()", javascript)
        self.assertIn(
            "beginToolBoundary();\n    appendToolCall",
            javascript,
        )
        self.assertIn(
            "placePendingTools(runtime.responseAssistantTurn)",
            javascript,
        )
        self.assertIn("function placePendingTools(turn)", javascript)
        self.assertNotIn(
            "runtime.responseAssistantTurn = turn;\n    attachPendingTools(turn)",
            javascript,
        )

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

        transcript = self.client.get("/sessions/json/messages")
        self.assertEqual(transcript.status_code, 200)
        self.assertEqual(
            transcript.json(),
            {
                "sessionId": "json",
                "userId": NEUTRAL_USER_ID,
                "messages": [
                    {
                        "id": f"{request.invocation_id}:user",
                        "role": "user",
                        "content": "hello",
                        "createdAt": None,
                        "metadata": {"fake": {"kind": "input"}},
                    },
                    {
                        "id": f"{request.invocation_id}:assistant",
                        "role": "assistant",
                        "content": "hello",
                        "createdAt": None,
                        "metadata": {"fake": {"kind": "output"}},
                    },
                ],
                "nextCursor": None,
            },
        )
        self.assertEqual(
            self.client.get("/sessions/missing/messages").status_code,
            404,
        )

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

    def test_session_messages_support_optional_cursor_pagination(self):
        created = self.client.post("/sessions", json={"id": "paginated"})
        self.assertEqual(created.status_code, 201)
        self.driver.session_messages[(NEUTRAL_USER_ID, "paginated")] = [
            SessionMessage(
                id=f"message-{index}",
                role="assistant",
                content=f"content-{index}",
            )
            for index in range(4)
        ]

        complete = self.client.get("/sessions/paginated/messages")
        self.assertEqual(complete.status_code, 200)
        self.assertIsNone(complete.json()["nextCursor"])
        self.assertEqual(len(complete.json()["messages"]), 4)

        first = self.client.get(
            "/sessions/paginated/messages", params={"limit": 2}
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(
            [item["id"] for item in first.json()["messages"]],
            ["message-0", "message-1"],
        )
        first_cursor = first.json()["nextCursor"]
        self.assertIsInstance(first_cursor, str)

        second = self.client.get(
            "/sessions/paginated/messages",
            params={"limit": 1, "cursor": first_cursor},
        )
        self.assertEqual(
            [item["id"] for item in second.json()["messages"]],
            ["message-2"],
        )
        second_cursor = second.json()["nextCursor"]
        self.assertIsInstance(second_cursor, str)

        final = self.client.get(
            "/sessions/paginated/messages", params={"cursor": second_cursor}
        )
        self.assertEqual(
            [item["id"] for item in final.json()["messages"]],
            ["message-3"],
        )
        self.assertIsNone(final.json()["nextCursor"])
        self.assertEqual(
            self.client.get(
                "/sessions/paginated/messages", params={"cursor": "invalid"}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.get(
                "/sessions/paginated/messages", params={"limit": 101}
            ).status_code,
            422,
        )

    def test_default_message_page_is_bounded(self):
        created = self.client.post("/sessions", json={"id": "bounded"})
        self.assertEqual(created.status_code, 201)
        self.driver.session_messages[(NEUTRAL_USER_ID, "bounded")] = [
            SessionMessage(
                id=f"message-{index:03d}",
                role="assistant",
                content=f"content-{index}",
            )
            for index in range(102)
        ]

        first = self.client.get("/sessions/bounded/messages").json()
        self.assertEqual(len(first["messages"]), 100)
        self.assertEqual(first["messages"][-1]["id"], "message-099")
        self.assertIsInstance(first["nextCursor"], str)
        final = self.client.get(
            "/sessions/bounded/messages",
            params={"cursor": first["nextCursor"]},
        ).json()
        self.assertEqual(
            [item["id"] for item in final["messages"]],
            ["message-100", "message-101"],
        )
        self.assertIsNone(final["nextCursor"])

    def test_message_cursor_is_resource_bound_and_detects_prefix_mutation(self):
        for session_id in ("source", "other"):
            created = self.client.post("/sessions", json={"id": session_id})
            self.assertEqual(created.status_code, 201)
            self.driver.session_messages[(NEUTRAL_USER_ID, session_id)] = [
                SessionMessage(id="shared-0", role="user", content="first"),
                SessionMessage(id="shared-1", role="assistant", content="second"),
                SessionMessage(id="shared-2", role="assistant", content="third"),
            ]

        first = self.client.get(
            "/sessions/source/messages", params={"limit": 2}
        ).json()
        cursor = first["nextCursor"]
        cross_session = self.client.get(
            "/sessions/other/messages", params={"limit": 1, "cursor": cursor}
        )
        self.assertEqual(cross_session.status_code, 400)

        self.driver.session_messages[(NEUTRAL_USER_ID, "source")][0] = (
            SessionMessage(id="changed", role="user", content="first")
        )
        stale = self.client.get(
            "/sessions/source/messages", params={"limit": 1, "cursor": cursor}
        )
        self.assertEqual(stale.status_code, 400)

    def test_message_cursor_handles_duplicate_ids_append_and_empty_transcript(self):
        created = self.client.post("/sessions", json={"id": "changing"})
        self.assertEqual(created.status_code, 201)
        messages = [
            SessionMessage(id="duplicate", role="user", content="first"),
            SessionMessage(id="duplicate", role="assistant", content="second"),
            SessionMessage(id="third", role="assistant", content="third"),
        ]
        self.driver.session_messages[(NEUTRAL_USER_ID, "changing")] = messages

        first = self.client.get(
            "/sessions/changing/messages", params={"limit": 1}
        ).json()
        second = self.client.get(
            "/sessions/changing/messages",
            params={"limit": 1, "cursor": first["nextCursor"]},
        ).json()
        self.assertEqual(second["messages"][0]["content"], "second")

        messages.append(
            SessionMessage(id="fourth", role="assistant", content="fourth")
        )
        remaining = self.client.get(
            "/sessions/changing/messages",
            params={"cursor": second["nextCursor"]},
        ).json()
        self.assertEqual(
            [item["id"] for item in remaining["messages"]],
            ["third", "fourth"],
        )
        self.assertIsNone(remaining["nextCursor"])

        empty = self.client.post("/sessions", json={"id": "empty"})
        self.assertEqual(empty.status_code, 201)
        empty_page = self.client.get(
            "/sessions/empty/messages", params={"limit": 10}
        ).json()
        self.assertEqual(empty_page["messages"], [])
        self.assertIsNone(empty_page["nextCursor"])

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

    def test_response_request_is_strict_and_documented_by_pydantic(self):
        schema = self.client.get("/openapi.json").json()
        request_schema = schema["paths"]["/responses"]["post"]["requestBody"][
            "content"
        ]["application/json"]["schema"]

        self.assertEqual(request_schema["additionalProperties"], False)
        self.assertIn("sessionId", request_schema["properties"])
        self.assertEqual(
            self.client.post(
                "/responses", json={"input": "hello", "stream": "true"}
            ).status_code,
            400,
        )

    def test_structured_input_is_shared_by_json_sse_and_websocket(self):
        driver = StructuredInputDriver()
        with TestClient(create_neutral_app(driver)) as client:
            client.post("/sessions", json={"id": "structured"})
            input_value = {"query": "status", "limit": 2}

            response = client.post(
                "/responses",
                json={"input": input_value, "sessionId": "structured"},
            )
            stream = client.post(
                "/responses",
                json={
                    "input": input_value,
                    "sessionId": "structured",
                    "stream": True,
                },
            )
            with client.websocket_connect("/live") as websocket:
                websocket.send_json({"type": "connect", "sessionId": "structured"})
                websocket.receive_json()
                websocket.send_json(
                    {"type": "response.create", "input": input_value}
                )
                while websocket.receive_json()["type"] != "response.completed":
                    pass

            invalid = client.post(
                "/responses",
                json={
                    "input": {"query": "status", "limit": "2"},
                    "sessionId": "structured",
                },
            )
            schema = client.get("/openapi.json").json()["paths"]["/responses"][
                "post"
            ]["requestBody"]["content"]["application/json"]["schema"]

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: response.completed", stream.text)
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(driver.invocations[0].input, input_value)
        self.assertEqual(driver.invocations[1].input, input_value)
        self.assertEqual(driver.invocations[2].input, input_value)
        self.assertIn("$defs", schema)

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
            client.post("/sessions", headers=alice, json={"id": "shared-2"})
            alice_page = client.get(
                "/sessions", headers=alice, params={"limit": 1}
            ).json()
            self.assertEqual(
                client.get(
                    "/sessions",
                    headers=bob,
                    params={"cursor": alice_page["nextCursor"]},
                ).status_code,
                400,
            )
            self.assertEqual(
                client.get("/sessions/shared", headers=bob).status_code,
                404,
            )
            self.assertEqual(
                client.get("/sessions/shared/messages", headers=bob).status_code,
                404,
            )
            response = client.post(
                "/responses",
                headers=alice,
                json={"input": "hello", "sessionId": "shared"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(driver.invocations[-1].user_id, "alice")
            transcript = client.get(
                "/sessions/shared/messages", headers=alice
            )
            self.assertEqual(transcript.status_code, 200)
            self.assertEqual(transcript.json()["userId"], "alice")
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

    def test_dynamic_approval_runs_only_after_evaluation_and_does_not_replay_it(self):
        driver = DynamicApprovalDriver()
        app = create_neutral_app(driver)
        with TestClient(app) as client:
            client.post("/sessions", json={"id": "dynamic"})
            safe = client.post(
                "/responses", json={"input": "safe", "sessionId": "dynamic"}
            ).json()
            required = client.post(
                "/responses", json={"input": "risky", "sessionId": "dynamic"}
            ).json()

            self.assertEqual(safe["status"], "completed")
            self.assertEqual(required["status"], "requires_action")
            self.assertEqual(
                required["requiredAction"]["action"],
                "dynamic:typescript.execute",
            )
            self.assertEqual(driver.evaluations, 2)
            self.assertEqual(driver.executions, 0)

            completed = client.post(
                f"/approvals/{required['requiredAction']['id']}",
                json={"decision": "approve"},
            ).json()

            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["outputText"], "executed:typescript")
            self.assertEqual(driver.evaluations, 2)
            self.assertEqual(driver.executions, 1)

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


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi is not installed")
class ClientToolTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = TestClient(create_neutral_app(ClientToolDriver()))
        self.client = self.context.__enter__()
        self.client.post("/sessions", json={"id": "client-session"})

    def tearDown(self) -> None:
        self.context.__exit__(None, None, None)

    def test_json_suspends_and_resumes_with_client_output(self):
        with self.assertLogs("harnest.agent.client_tool.audit", level="INFO") as audit:
            required = self.client.post(
                "/responses",
                json={
                    "input": "https://example.test",
                    "sessionId": "client-session",
                },
            ).json()

            self.assertEqual(required["status"], "requires_action")
            action = required["requiredAction"]
            self.assertEqual(action["type"], "client_tool")
            self.assertEqual(action["name"], "_browser_open")
            self.assertEqual(action["arguments"], {"url": "https://example.test"})
            completed = self.client.post(
                f"/client-tools/{action['id']}",
                json={"output": {"title": "Example"}},
            )

        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.json()["outputText"], "opened:Example")
        self.assertTrue(any("client_tool.requested" in item for item in audit.output))
        self.assertTrue(
            any("client_tool.result_submitted" in item for item in audit.output)
        )

    def test_sse_exposes_the_same_client_tool_contract(self):
        response = self.client.post(
            "/responses",
            json={
                "input": "https://example.test",
                "sessionId": "client-session",
                "stream": True,
            },
        )

        self.assertIn("event: client_tool.requested", response.text)
        self.assertIn('"type": "client_tool"', response.text)

    def test_live_round_trip_resumes_on_the_same_socket(self):
        with self.client.websocket_connect("/live") as websocket:
            websocket.send_json({"type": "connect", "sessionId": "client-session"})
            websocket.receive_json()
            websocket.send_json(
                {"type": "response.create", "input": "https://example.test"}
            )
            self.assertEqual(websocket.receive_json()["type"], "response.created")
            requested = websocket.receive_json()
            self.assertEqual(requested["type"], "client_tool.requested")
            websocket.send_json(
                {
                    "type": "client_tool.result",
                    "requestId": requested["clientTool"]["id"],
                    "output": {"title": "Live Example"},
                }
            )
            completed = websocket.receive_json()
            self.assertEqual(completed["type"], "response.text.delta")
            completed = websocket.receive_json()
            self.assertEqual(completed["type"], "response.completed")
            self.assertEqual(completed["outputText"], "opened:Live Example")

    def test_approved_client_tool_resumes_through_both_boundaries(self):
        app = create_neutral_app(ApprovedClientToolDriver())
        with TestClient(app) as client:
            client.post("/sessions", json={"id": "approved-client"})
            approval = client.post(
                "/responses",
                json={"input": "https://example.test", "sessionId": "approved-client"},
            ).json()
            client_action = client.post(
                f"/approvals/{approval['requiredAction']['id']}",
                json={"decision": "approve"},
            ).json()
            completed = client.post(
                f"/client-tools/{client_action['requiredAction']['id']}",
                json={"output": {"title": "Approved Example"}},
            )

        self.assertEqual(approval["requiredAction"]["type"], "human_approval")
        self.assertEqual(client_action["requiredAction"]["type"], "client_tool")
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.json()["outputText"], "approved-opened:Approved Example")


if __name__ == "__main__":
    unittest.main()
