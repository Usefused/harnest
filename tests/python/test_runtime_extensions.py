import asyncio
import unittest
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from typing import AsyncIterator, Mapping, Sequence
from unittest.mock import patch

from harnest.lifecycle import LifecycleListener
from harnest.context import (
    ContextResourceError,
    ContextUnavailableError,
    ContextValue,
    context,
)
from harnest.context_session import invocation_session_context
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
    RuntimeResourceError,
    StreamingResultTransformationError,
    LifecycleAuthenticator,
)
from harnest.runtime_auth import AuthPrincipal, AuthenticationError, ConnectionContext
from harnest.session import InMemorySessionStore
from harnest.skills import (
    SkillDescriptor,
    SkillDocument,
    SkillPage,
    SkillRegistry,
    SkillScope,
    SkillSource,
)


class RuntimeSkillSource(SkillSource):
    """Expose active identity to verify runtime registry propagation."""

    async def list(self, active, *, query=None, cursor=None, limit=50):
        return SkillPage(
            (SkillDescriptor("generated", "generated", active.agent_name, "v1"),)
        )

    async def load(self, skill_id, active, *, version=None):
        descriptor = SkillDescriptor(skill_id, skill_id, active.agent_name, "v1")
        return SkillDocument(descriptor, "Generated instructions.")


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

    async def list_sessions(
        self,
        *,
        user_id: str,
        after: str | None = None,
        limit: int | None = None,
    ) -> Sequence[SessionRecord]:
        del after, limit
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


def listener(phase, callback, *, name="hook", order=0, context_name=None):
    return LifecycleListener(
        phase,
        callback,
        order,
        f"{name}.py",
        1,
        name,
        context_name=context_name,
    )


class ExtensionRuntimeDriverTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_invocation_canonicalizes_without_empty_plugin_work(self):
        """Keep empty capability optimization behind the same result contract."""

        driver = FakeDriver()
        original = InvocationResult(
            text="stale",
            events=tuple(driver._events()),
            result="stale",
            session_id="session-1",
            metadata={},
        )

        async def invoke(_request):
            return original

        driver.invoke = invoke  # type: ignore[method-assign]
        wrapped = ExtensionRuntimeDriver(driver, [])
        with (
            patch(
                "harnest.plugin_runtime_context.activate_plugin_bindings",
                side_effect=AssertionError("empty plugins must not bind"),
            ),
            patch(
                "harnest.plugin_runtime_context.revoke_plugin_bindings",
                side_effect=AssertionError("empty plugins must not revoke"),
            ),
        ):
            result = await wrapped.invoke(request())

        self.assertEqual(result.text, "hello")
        self.assertEqual(result.events, tuple(driver._events()))
        self.assertEqual(result.result, 3)

    async def test_context_skills_receives_the_compiled_runtime_registry(self):
        """Make lifecycle hooks and framework tools share one invocation scope."""

        seen = []

        async def before(_lifecycle, value):
            page = await context.skills.list(source="wex")
            seen.append(page.items[0].descriptor.description)
            return value

        registry = SkillRegistry(
            {"support": SkillScope({"wex": RuntimeSkillSource()})}
        )
        wrapped = ExtensionRuntimeDriver(
            FakeDriver(),
            [listener("before_invoke", before)],
            skill_registry=registry,
        )

        await wrapped.invoke(request())

        self.assertEqual(seen, ["support"])

    async def test_session_context_spans_agent_hooks_and_reuses_backend_lease(self):
        """Keep application data available without nested session acquisition."""

        store = InMemorySessionStore()
        await store.start()
        await store.create(session_id="session-1", user_id="user-1", state={})
        acquisitions = 0
        original_acquire = store.acquire

        @asynccontextmanager
        async def counted_acquire(*, session_id, user_id):
            nonlocal acquisitions
            acquisitions += 1
            async with original_acquire(
                session_id=session_id, user_id=user_id
            ) as lease:
                yield lease

        store.acquire = counted_acquire  # type: ignore[method-assign]

        class SessionDriver(FakeDriver):
            async def invoke(self, invocation):
                async with invocation_session_context(
                    store,
                    framework="adk",
                    user_id=invocation.user_id,
                    session_id=invocation.session_id,
                    invocation_id=invocation.invocation_id,
                ) as lease:
                    self.lease = lease
                    await context.session.set("driver", True)
                return await super().invoke(invocation)

        async def before(lifecycle_context, value):
            await context.session.set("before", lifecycle_context.invocation_id)
            return value

        async def after(lifecycle_context, result):
            await context.session.set("after", lifecycle_context.invocation_id)
            return result

        driver = SessionDriver()
        wrapped = ExtensionRuntimeDriver(
            driver,
            [
                listener("before_invoke", before, name="before"),
                listener("after_invoke", after, name="after"),
            ],
            session_store=store,
        )

        await wrapped.invoke(request())
        record = await store.get(session_id="session-1", user_id="user-1")

        self.assertEqual(acquisitions, 1)
        self.assertEqual(
            record.application_data,
            {"before": "invoke-1", "driver": True, "after": "invoke-1"},
        )
        await wrapped.close()

    async def test_session_context_is_available_to_agent_error_hooks(self):
        """Let failure handlers persist bounded recovery state before lease release."""

        store = InMemorySessionStore()
        await store.start()
        await store.create(session_id="session-1", user_id="user-1", state={})
        driver = FakeDriver()
        driver.fail = True

        async def on_error(_lifecycle_context, _error):
            await context.session.set("failed", True)

        wrapped = ExtensionRuntimeDriver(
            driver,
            [listener("on_error", on_error, name="failed")],
            session_store=store,
        )

        with self.assertRaisesRegex(RuntimeError, "driver failed"):
            await wrapped.invoke(request())
        record = await store.get(session_id="session-1", user_id="user-1")

        self.assertEqual(record.application_data, {"failed": True})
        await wrapped.close()

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

    async def test_lifecycle_identity_does_not_use_public_display_name(self):
        """Keep agent routing stable when a card customizes its display name."""

        driver = FakeDriver()
        driver._info = replace(
            driver.info,
            id="boundary_root",
            name="Friendly Boundary Agent",
        )
        seen = []

        def before(lifecycle_context, invocation):
            seen.append((lifecycle_context.agent_name, context.agent_name))
            return lifecycle_context.next(invocation)

        wrapped = ExtensionRuntimeDriver(
            driver,
            [listener("before_invoke", before, name="identity")],
        )

        await wrapped.invoke(request())

        self.assertEqual(seen, [("boundary_root", "boundary_root")])

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

    async def test_runtime_resources_enter_once_and_unwind_after_driver_close(self):
        driver = FakeDriver()
        events = []

        @contextmanager
        def sync_resource():
            events.append("sync-start")
            try:
                yield
            finally:
                events.append(f"sync-stop:{driver.closed}")

        @asynccontextmanager
        async def async_resource():
            events.append("async-start")
            try:
                yield
            finally:
                events.append(f"async-stop:{driver.closed}")

        wrapped = ExtensionRuntimeDriver(
            driver,
            [
                listener("resource", sync_resource, name="sync", order=1),
                listener("resource", async_resource, name="async", order=2),
            ],
        )

        await wrapped.create_session(
            session_id="session-1", user_id="user-1", state={}
        )
        await wrapped.invoke(request())
        await wrapped.close()
        await wrapped.close()

        self.assertEqual(
            events,
            [
                "sync-start",
                "async-start",
                "async-stop:True",
                "sync-stop:True",
            ],
        )

    async def test_plain_generator_resources_are_managed_without_boilerplate(self):
        events = []

        def sync_resource():
            events.append("sync-start")
            try:
                yield "sync"
            finally:
                events.append("sync-stop")

        async def async_resource():
            events.append("async-start")
            try:
                yield "async"
            finally:
                events.append("async-stop")

        wrapped = ExtensionRuntimeDriver(
            FakeDriver(),
            [
                listener("resource", sync_resource, name="sync"),
                listener("resource", async_resource, name="async"),
            ],
        )

        await wrapped.invoke(request())
        await wrapped.close()

        self.assertEqual(
            events, ["sync-start", "async-start", "async-stop", "sync-stop"]
        )

    async def test_context_resource_is_visible_to_hooks_driver_and_child_tasks(self):
        memory = object()
        seen = []

        async def managed_memory():
            seen.append("start")
            try:
                yield memory
            finally:
                seen.append("stop")

        class ContextDriver(FakeDriver):
            async def invoke(self, invocation):
                async def child():
                    return context.resource("memory")

                seen.append(context.resource("memory"))
                seen.append(await asyncio.create_task(child()))
                return await super().invoke(invocation)

        def before(_context, value):
            seen.append(context.resource("memory"))
            return value

        wrapped = ExtensionRuntimeDriver(
            ContextDriver(),
            [
                listener(
                    "resource",
                    managed_memory,
                    name="memory",
                    context_name="memory",
                ),
                listener("before_invoke", before, name="before"),
            ],
        )

        await wrapped.invoke(request())
        with self.assertRaises(ContextUnavailableError):
            context.resource("memory")
        await wrapped.close()

        self.assertEqual(seen, ["start", memory, memory, memory, "stop"])

    async def test_context_only_provider_is_resolved_once_per_invocation(self):
        created = []

        async def request_cache():
            value = object()
            created.append(value)
            return value

        class ContextDriver(FakeDriver):
            async def invoke(self, invocation):
                self.asserted = context.resource("request_cache")
                return await super().invoke(invocation)

        driver = ContextDriver()
        wrapped = ExtensionRuntimeDriver(
            driver,
            [
                listener(
                    "context",
                    request_cache,
                    name="request_cache",
                    context_name="request_cache",
                )
            ],
        )

        await wrapped.invoke(request())
        first = driver.asserted
        await wrapped.invoke(replace(request(), invocation_id="invoke-2"))

        self.assertEqual(len(created), 2)
        self.assertIs(first, created[0])
        self.assertIs(driver.asserted, created[1])
        await wrapped.close()

    async def test_context_values_expose_prebuilt_storage_explicitly(self):
        sessions = object()

        class ContextDriver(FakeDriver):
            async def invoke(self, invocation):
                self.sessions = context.resource("sessions")
                return await super().invoke(invocation)

        driver = ContextDriver()
        wrapped = ExtensionRuntimeDriver(
            driver,
            [],
            context_values=(ContextValue("sessions", sessions, "storage.py:1"),),
        )

        await wrapped.invoke(request())

        self.assertIs(driver.sessions, sessions)
        await wrapped.close()

    async def test_stream_context_does_not_leak_to_frame_consumer(self):
        memory = object()

        class ContextDriver(FakeDriver):
            async def stream(self, invocation):
                self.memory = context.resource("memory")
                async for event in super().stream(invocation):
                    yield event

        driver = ContextDriver()
        wrapped = ExtensionRuntimeDriver(
            driver,
            [],
            context_values=(ContextValue("memory", memory, "memory.py:1"),),
        )

        count = 0
        async for _ in wrapped.stream(request()):
            count += 1
            with self.assertRaises(ContextUnavailableError):
                context.resource("memory")

        self.assertEqual(count, 3)
        self.assertIs(driver.memory, memory)
        await wrapped.close()

    async def test_child_task_cannot_retain_context_after_invocation(self):
        release = asyncio.Event()

        class ContextDriver(FakeDriver):
            async def invoke(self, invocation):
                async def background():
                    await release.wait()
                    return context.resource("memory")

                self.background = asyncio.create_task(background())
                return await super().invoke(invocation)

        driver = ContextDriver()
        wrapped = ExtensionRuntimeDriver(
            driver,
            [],
            context_values=(ContextValue("memory", object(), "memory.py:1"),),
        )

        await wrapped.invoke(request())
        release.set()

        with self.assertRaises(ContextUnavailableError):
            await driver.background
        await wrapped.close()

    def test_runtime_rejects_duplicate_or_unnamed_context_providers(self):
        provider = listener(
            "context",
            lambda: object(),
            name="cache",
            context_name="cache",
        )
        with self.assertRaisesRegex(ValueError, "duplicate names"):
            ExtensionRuntimeDriver(
                FakeDriver(),
                [provider],
                context_values=(ContextValue("cache", object(), "storage.py:1"),),
            )
        with self.assertRaisesRegex(ValueError, "require a context name"):
            ExtensionRuntimeDriver(
                FakeDriver(),
                [listener("context", lambda: object(), name="unnamed")],
            )

    async def test_private_lifecycle_resource_is_not_published(self):
        @contextmanager
        def private_resource():
            yield object()

        class ContextDriver(FakeDriver):
            async def invoke(self, invocation):
                with self.test.assertRaises(ContextResourceError):
                    context.resource("private")
                return await super().invoke(invocation)

        driver = ContextDriver()
        driver.test = self
        wrapped = ExtensionRuntimeDriver(
            driver,
            [listener("resource", private_resource, name="private")],
        )

        await wrapped.invoke(request())
        await wrapped.close()

    async def test_invalid_runtime_resource_fails_before_driver_execution(self):
        driver = FakeDriver()
        wrapped = ExtensionRuntimeDriver(
            driver,
            [listener("resource", lambda: object(), name="invalid")],
        )

        with self.assertRaisesRegex(RuntimeResourceError, "context manager"):
            await wrapped.invoke(request())

        self.assertEqual(driver.requests, [])
        await wrapped.close()

    async def test_close_without_use_does_not_create_runtime_resources(self):
        created = []
        wrapped = ExtensionRuntimeDriver(
            FakeDriver(),
            [
                listener(
                    "resource",
                    lambda: created.append(True),
                    name="never_started",
                )
            ],
        )

        await wrapped.close()

        self.assertEqual(created, [])


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
