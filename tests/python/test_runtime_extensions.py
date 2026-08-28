import asyncio
import unittest
from dataclasses import replace
from typing import AsyncIterator, Mapping, Sequence

from harnest.lifecycle import LifecycleListener
from harnest.neutral_runtime import (
    AgentInfo,
    InvocationRequest,
    InvocationResult,
    RuntimeEvent,
    SessionRecord,
)
from harnest.runtime_extensions import (
    DROP_EVENT,
    ExtensionRuntimeDriver,
    ExtensionTransformError,
    StreamingResultTransformationError,
    LifecycleAuthenticator,
)
from harnest.runtime_auth import AuthPrincipal, AuthenticationError, ConnectionContext


class FakeDriver:
    def __init__(self) -> None:
        self._info = AgentInfo(
            id="support",
            name="support",
            description="Support",
            card={},
            framework="adk",
            mode="managed",
        )
        self.requests = []
        self.closed = False
        self.fail = False

    @property
    def info(self) -> AgentInfo:
        return self._info

    async def create_session(
        self, *, session_id: str, user_id: str, state: Mapping[str, object]
    ) -> SessionRecord:
        return SessionRecord(id=session_id, user_id=user_id, state=state)

    async def get_session(
        self, *, session_id: str, user_id: str
    ) -> SessionRecord | None:
        return SessionRecord(id=session_id, user_id=user_id, state={})

    async def list_sessions(self, *, user_id: str) -> Sequence[SessionRecord]:
        return [SessionRecord(id="session-1", user_id=user_id, state={})]

    async def update_session(
        self,
        *,
        session_id: str,
        user_id: str,
        state_delta: Mapping[str, object],
    ) -> SessionRecord | None:
        return SessionRecord(id=session_id, user_id=user_id, state=state_delta)

    async def delete_session(self, *, session_id: str, user_id: str) -> bool:
        return True

    def _events(self) -> list[RuntimeEvent]:
        return [
            {"type": "message", "text": "hello"},
            {"type": "tool_call", "name": "hidden"},
            {"type": "graph_output", "output": 3},
        ]

    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("driver failed")
        return InvocationResult(
            text="hello",
            events=self._events(),
            result=3,
            session_id=request.session_id,
            metadata=request.metadata,
        )

    async def stream(
        self, request: InvocationRequest
    ) -> AsyncIterator[RuntimeEvent]:
        self.requests.append(request)
        for event in self._events():
            await asyncio.sleep(0)
            yield event
        if self.fail:
            raise RuntimeError("stream failed")

    async def close(self) -> None:
        self.closed = True


def request() -> InvocationRequest:
    return InvocationRequest(
        input="raw",
        user_id="user-1",
        session_id="session-1",
        invocation_id="invoke-1",
        metadata={"source": "test"},
        state_delta={},
    )


def listener(phase, callback, *, name="hook", order=0):
    return LifecycleListener(phase, callback, order, f"{name}.py", 1, name)


class ExtensionRuntimeDriverTests(unittest.IsolatedAsyncioTestCase):
    async def test_invoke_runs_transformations_in_order_with_one_context(self):
        driver = FakeDriver()
        seen = []

        def before(context, value):
            context.attributes["guarded"] = True
            seen.append(("before", value.input))
            return replace(value, input="checked")

        async def on_event(context, event):
            self.assertTrue(context.attributes["guarded"])
            if event["type"] == "tool_call":
                return DROP_EVENT
            if event["type"] == "message":
                return {**event, "text": event["text"].upper()}
            return None

        def after(context, result):
            seen.append(("after", context.invocation_id))
            return replace(result, text=result.text + "!")

        wrapped = ExtensionRuntimeDriver(
            driver,
            [
                listener("before_invoke", before, name="guardrails_before"),
                listener("on_event", on_event, name="guardrails_event"),
                listener("after_invoke", after, name="guardrails_after"),
            ],
        )

        result = await wrapped.invoke(request())

        self.assertEqual(driver.requests[0].input, "checked")
        self.assertEqual(result.text, "HELLO!")
        self.assertEqual(
            result.events,
            (
                {"type": "message", "text": "HELLO"},
                {"type": "graph_output", "output": 3},
            ),
        )
        self.assertEqual(seen, [("before", "raw"), ("after", "invoke-1")])

    async def test_stream_transforms_events_and_after_is_observational(self):
        driver = FakeDriver()
        observed = []

        async def on_event(_context, event):
            if event["type"] == "tool_call":
                return DROP_EVENT
            return {**event, "observed": True}

        def after(_context, result):
            observed.append(result)

        wrapped = ExtensionRuntimeDriver(
            driver,
            [
                listener("on_event", on_event, name="history_event"),
                listener("after_invoke", after, name="history_after"),
            ],
        )

        events = [event async for event in wrapped.stream(request())]

        self.assertEqual(len(events), 2)
        self.assertTrue(all(event["observed"] for event in events))
        self.assertEqual(observed[0].events, tuple(events))
        self.assertEqual(observed[0].text, "hello")
        self.assertEqual(observed[0].result, 3)

    async def test_stream_rejects_after_result_replacement(self):
        driver = FakeDriver()
        wrapped = ExtensionRuntimeDriver(
            driver,
            [listener("after_invoke", lambda _ctx, result: result, name="invalid")],
        )

        with self.assertRaisesRegex(
            StreamingResultTransformationError, "after_invoke cannot replace"
        ):
            _ = [event async for event in wrapped.stream(request())]

    async def test_errors_notify_every_extension_without_masking_original(self):
        driver = FakeDriver()
        driver.fail = True
        notified = []

        async def first(_context, error):
            notified.append(("first", str(error)))
            raise RuntimeError("notification failed")

        def second(_context, error):
            notified.append(("second", str(error)))

        wrapped = ExtensionRuntimeDriver(
            driver,
            [
                listener("on_error", first, name="first"),
                listener("on_error", second, name="second"),
            ],
        )

        with self.assertRaisesRegex(RuntimeError, "driver failed"):
            await wrapped.invoke(request())
        self.assertEqual(
            notified,
            [("first", "driver failed"), ("second", "driver failed")],
        )

    async def test_invalid_hook_replacements_are_rejected_and_notified(self):
        notified = []
        wrapped = ExtensionRuntimeDriver(
            FakeDriver(),
            [
                listener(
                    "before_invoke",
                    lambda _context, _request: "wrong",
                    name="invalid",
                ),
                listener(
                    "on_error",
                    lambda _context, error: notified.append(str(error)),
                    name="notify",
                ),
            ],
        )

        with self.assertRaisesRegex(
            ExtensionTransformError, "InvocationRequest or None"
        ):
            await wrapped.invoke(request())
        self.assertEqual(len(notified), 1)

    async def test_session_and_close_operations_are_forwarded(self):
        driver = FakeDriver()
        wrapped = ExtensionRuntimeDriver(driver, [])

        session = await wrapped.create_session(
            session_id="session-1", user_id="user-1", state={"ready": True}
        )
        self.assertEqual(session.state, {"ready": True})
        self.assertEqual((await wrapped.list_sessions(user_id="user-1"))[0].id, "session-1")
        self.assertTrue(
            await wrapped.delete_session(session_id="session-1", user_id="user-1")
        )
        await wrapped.close()
        self.assertTrue(driver.closed)


class LifecycleAuthenticatorTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def context():
        return ConnectionContext(
            transport="http",
            method="POST",
            path="/responses",
            headers={"x-user": "martins"},
            cookies={},
            query={},
        )

    async def test_multiple_listeners_resolve_then_enrich_one_identity(self):
        async def resolve(connection, principal):
            self.assertIsNone(principal)
            return AuthPrincipal(connection.headers["x-user"])

        def enrich(_connection, principal):
            return AuthPrincipal(principal.user_id, {"team": "runtime"})

        authenticator = LifecycleAuthenticator(
            [
                listener("authenticate", resolve, name="resolve"),
                listener("authenticate", enrich, name="enrich"),
            ]
        )
        principal = await authenticator.authenticate(self.context())
        self.assertEqual(principal.user_id, "martins")
        self.assertEqual(principal.claims, {"team": "runtime"})

    async def test_authentication_fails_closed_and_identity_cannot_change(self):
        empty = LifecycleAuthenticator(
            [listener("authenticate", lambda _connection, _principal: None)]
        )
        with self.assertRaises(AuthenticationError):
            await empty.authenticate(self.context())

        changed = LifecycleAuthenticator(
            [
                listener(
                    "authenticate",
                    lambda _connection, _principal: AuthPrincipal("one"),
                    name="one",
                ),
                listener(
                    "authenticate",
                    lambda _connection, _principal: AuthPrincipal("two"),
                    name="two",
                ),
            ]
        )
        with self.assertRaisesRegex(AuthenticationError, "rejected"):
            await changed.authenticate(self.context())

    def test_connection_context_is_read_only(self):
        context = self.context()
        with self.assertRaises(TypeError):
            context.headers["x-user"] = "spoofed"


if __name__ == "__main__":
    unittest.main()
