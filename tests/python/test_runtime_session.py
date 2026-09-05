import asyncio
from contextlib import asynccontextmanager
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from harnest.application import CompiledApplication
from harnest.checkpoint import (
    ADKStore,
    MemoryStore,
    RunScope,
    get_durable_run_result,
)
from harnest.neutral_runtime import AgentInfo, SessionRecord
from harnest.output import OutputPolicy, TokenUsage
from harnest.runtime import (
    AgentRuntimeError,
    _attach_driver_lifecycle,
    _runtime_driver,
)
from harnest.runtime_extensions import ExtensionRuntimeDriver
from harnest.runtime_contract import InvocationRequest, InvocationResult
from harnest.runtime_session import StorageRuntimeDriver
from harnest.session import InMemorySessionStore
from harnest.storage_registry import StorageRegistry


class RecordingStore(InMemorySessionStore):
    def __init__(self):
        super().__init__()
        self.started = 0
        self.closed = 0

    async def start(self):
        self.started += 1

    async def close(self):
        self.closed += 1
        await super().close()


class RecordingResource:
    """Expose partial startup and cleanup ordering without a real backend."""

    def __init__(
        self,
        name,
        events,
        *,
        start_error=None,
        close_error=None,
    ):
        self.name = name
        self.events = events
        self.start_error = start_error
        self.close_error = close_error

    async def start(self):
        """Record lifecycle entry before simulating partial initialization."""

        self.events.append(f"start:{self.name}")
        if self.start_error is not None:
            raise self.start_error

    async def close(self):
        """Record the matching ownership release and optional failure."""

        self.events.append(f"close:{self.name}")
        if self.close_error is not None:
            raise self.close_error


def _driver():
    info = AgentInfo(
        id="root",
        name="root",
        description="",
        card={},
        framework="langgraph",
        mode="managed",
        extra_endpoints={},
    )
    record = SessionRecord(id="session", user_id="user", state={})
    return SimpleNamespace(
        info=info,
        create_session=AsyncMock(return_value=record),
        close=AsyncMock(),
    )


class _DurableBackend:
    """Begin one portable run and return deterministic public model metadata."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self.info = AgentInfo(
            id="root",
            name="root",
            description="",
            card={},
            framework="langgraph",
            mode="managed",
            extra_endpoints={},
        )

    async def invoke(self, request):
        """Create the active run normally owned by the framework adapter."""

        await self._begin(request)
        return InvocationResult(
            text="done",
            events=self._events(),
            result={"answer": 42},
            session_id=request.session_id,
            metadata={"caller": "metadata"},
        )

    async def stream(self, request):
        """Yield the same events so stream finalization exercises one contract."""

        await self._begin(request)
        for event in self._events():
            yield event

    async def close(self):
        """Expose the runtime driver lifecycle without owning the shared store."""

    async def _begin(self, request):
        """Claim a portable run using the backend's stable application identity."""

        await self.store.begin_run(
            application_id=self.info.id,
            user_id=request.user_id,
            session_id=request.session_id,
            run_id=request.invocation_id,
            framework="langgraph",
        )

    @staticmethod
    def _events():
        """Return one final answer and one raw-capable provider metadata event."""

        return (
            {"type": "message", "role": "assistant", "text": "done"},
            {
                "type": "agent_metadata",
                "framework": "langgraph",
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 2,
                    "total_tokens": 5,
                },
                "raw": {"provider_request_id": "request-secret"},
                "_raw_provider_metadata": True,
            },
            {"type": "graph_output", "output": {"answer": 42}},
        )


class SessionStoreRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_owned_store_starts_once_and_closes_once(self):
        store = RecordingStore()
        inner = _driver()
        driver = StorageRuntimeDriver(inner, store)

        for _ in range(2):
            await driver.create_session(
                session_id="session", user_id="user", state={}
            )
        await driver.close()
        await driver.close()

        self.assertEqual(store.started, 1)
        self.assertEqual(store.closed, 1)
        inner.close.assert_awaited_once()

    async def test_store_closes_when_backend_close_fails(self):
        store = RecordingStore()
        inner = _driver()
        inner.close.side_effect = RuntimeError("backend failed")
        driver = StorageRuntimeDriver(inner, store)
        await driver.start_owned_resources()

        with self.assertRaisesRegex(RuntimeError, "backend failed"):
            await driver.close()
        self.assertEqual(store.closed, 1)

    async def test_close_skips_resources_that_never_entered_startup(self):
        events = []
        resource = RecordingResource("unused", events)
        driver = StorageRuntimeDriver(_driver(), resource)

        await driver.close()

        self.assertEqual(events, [])

    async def test_partial_start_unwinds_only_attempted_resources_in_reverse(self):
        events = []
        startup = ValueError("primary-startup-detail")
        first = RecordingResource("first", events)
        failed = RecordingResource("failed", events, start_error=startup)
        untouched = RecordingResource("untouched", events)
        driver = StorageRuntimeDriver(_driver(), first, failed, untouched)

        with self.assertRaises(ValueError) as captured:
            await driver.start_owned_resources()

        self.assertIs(captured.exception, startup)
        self.assertEqual(
            events,
            ["start:first", "start:failed", "close:failed", "close:first"],
        )
        await driver.close()
        self.assertEqual(events.count("close:first"), 1)
        self.assertNotIn("start:untouched", events)

    async def test_startup_cleanup_failure_cannot_replace_primary_error(self):
        events = []
        startup = ValueError("primary-startup-detail")
        first = RecordingResource(
            "first",
            events,
            close_error=RuntimeError("private-first-cleanup"),
        )
        failed = RecordingResource(
            "failed",
            events,
            start_error=startup,
            close_error=OSError("private-failed-cleanup"),
        )
        driver = StorageRuntimeDriver(_driver(), first, failed)

        with self.assertRaises(ValueError) as captured:
            await driver.start_owned_resources()

        self.assertIs(captured.exception, startup)
        notes = getattr(captured.exception, "__notes__", ())
        self.assertTrue(any("storage startup cleanup" in note for note in notes))
        self.assertNotIn("private-failed-cleanup", " ".join(notes))
        self.assertEqual(
            events,
            ["start:first", "start:failed", "close:failed", "close:first"],
        )

    async def test_registry_starts_shared_and_custom_resources_once(self):
        """Deduplicate one object even when it fulfils several compiled roles."""

        store = RecordingStore()
        registry = StorageRegistry(
            sessions=store,
            checkpoints=None,
            custom={"users": store},
        )
        driver = StorageRuntimeDriver(_driver(), storage_registry=registry)

        await driver.create_session(session_id="session", user_id="user", state={})
        await driver.close()

        self.assertEqual(store.started, 1)
        self.assertEqual(store.closed, 1)

    async def test_invoke_persists_normalized_result_before_terminal_status(self):
        """Keep raw provider fields ephemeral unless persistence is opted in."""

        store = MemoryStore()
        registry = StorageRegistry(sessions=store, checkpoints=store)
        driver = StorageRuntimeDriver(
            _DurableBackend(store),
            storage_registry=registry,
            output_policy=OutputPolicy(agent_metadata="raw"),
        )
        request = InvocationRequest(
            input="hello",
            user_id="user",
            session_id="session",
            invocation_id="run-invoke",
            metadata={},
            state_delta={},
        )

        result = await driver.invoke(request)
        scope = RunScope("root", "user", "session", "run-invoke")
        run = await store.get_run(scope=scope)
        durable = await get_durable_run_result(store, scope=scope)

        self.assertEqual(result.result, {"answer": 42})
        self.assertEqual(run.status, "completed")
        self.assertEqual(durable.usage, TokenUsage(3, 2, 5))
        self.assertEqual(durable.metadata, {"caller": "metadata"})
        self.assertNotIn("raw", durable.agent_metadata[0])

    async def test_stream_can_explicitly_persist_raw_provider_metadata(self):
        """Retain raw metadata only under the separate durable-data opt-in."""

        store = MemoryStore()
        registry = StorageRegistry(sessions=store, checkpoints=store)
        driver = StorageRuntimeDriver(
            _DurableBackend(store),
            storage_registry=registry,
            output_policy=OutputPolicy(
                agent_metadata="raw", persist_raw_agent_metadata=True
            ),
        )
        request = InvocationRequest(
            input="hello",
            user_id="user",
            session_id="session",
            invocation_id="run-stream",
            metadata={"request": "metadata"},
            state_delta={},
        )

        events = [event async for event in driver.stream(request)]
        scope = RunScope("root", "user", "session", "run-stream")
        durable = await get_durable_run_result(store, scope=scope)

        self.assertEqual(len(events), 3)
        self.assertEqual(
            durable.agent_metadata[0]["raw"],
            {"provider_request_id": "request-secret"},
        )
        self.assertEqual(
            durable.completed_payload("run-stream", "session")["usage"],
            {
                "inputTokens": 3,
                "outputTokens": 2,
                "totalTokens": 5,
            },
        )


class NativeADKLifespanOwnershipTests(unittest.TestCase):
    def test_custom_lifespan_closes_owned_storage_once(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        events = []
        store = RecordingStore()
        inner = _driver()
        driver = StorageRuntimeDriver(inner, store)

        @asynccontextmanager
        async def adk_lifespan(_app):
            events.append("adk-start")
            try:
                yield
            finally:
                events.append(f"adk-stop:{inner.close.await_count}")

        app = FastAPI(lifespan=adk_lifespan)
        _attach_driver_lifecycle(app, driver)

        with TestClient(app):
            self.assertEqual(events, ["adk-start"])
        asyncio.run(driver.close())

        self.assertEqual(events, ["adk-start", "adk-stop:1"])
        self.assertEqual(store.closed, 1)
        inner.close.assert_awaited_once()


class SessionStoreRuntimeSelectionTests(unittest.TestCase):
    def test_langgraph_lifecycle_store_is_used_and_runtime_owned(self):
        store = RecordingStore()
        application = CompiledApplication(
            name="root",
            framework="langgraph",
            mode="managed",
            target=object(),
            session_store=store,
        )
        backend = _driver()
        with patch(
            "harnest.runtime_langgraph.LangGraphRuntimeDriver",
            return_value=backend,
        ) as constructor:
            selected = _runtime_driver(application)

        self.assertIsInstance(selected, StorageRuntimeDriver)
        self.assertIsInstance(selected._driver, ExtensionRuntimeDriver)
        self.assertIs(selected._driver._driver, backend)
        self.assertIs(constructor.call_args.kwargs["session_store"], store)

    def test_adk_lifecycle_store_is_adapted_and_runtime_owned(self):
        store = RecordingStore()
        application = CompiledApplication(
            name="root",
            framework="adk",
            mode="managed",
            target=object(),
            native_app=object(),
            session_store=store,
        )
        backend = _driver()
        service = object()
        with (
            patch(
                "harnest.session_adk.create_adk_session_service",
                return_value=service,
            ) as adapt,
            patch(
                "harnest.runtime_adk.ADKRuntimeDriver",
                return_value=backend,
            ) as constructor,
        ):
            selected = _runtime_driver(application)

        self.assertIsInstance(selected, StorageRuntimeDriver)
        self.assertIsInstance(selected._driver, ExtensionRuntimeDriver)
        adapt.assert_called_once_with(store)
        self.assertIs(constructor.call_args.kwargs["session_service"], service)

    def test_native_adk_store_uses_one_service_and_one_owned_resource(self):
        service = object()
        store = ADKStore(service)
        application = CompiledApplication(
            name="root",
            framework="adk",
            mode="advanced",
            target=object(),
            native_app=object(),
            session_store=store,
            checkpointer=store,
        )
        backend = _driver()
        with patch(
            "harnest.runtime_adk.ADKRuntimeDriver",
            return_value=backend,
        ) as constructor:
            selected = _runtime_driver(application)

        self.assertIsInstance(selected, StorageRuntimeDriver)
        self.assertIsInstance(selected._driver, ExtensionRuntimeDriver)
        self.assertEqual(selected._resources, (store,))
        self.assertIs(constructor.call_args.kwargs["session_service"], service)

    def test_lifecycle_store_conflicts_with_host_injection(self):
        store = RecordingStore()
        application = CompiledApplication(
            name="root",
            framework="langgraph",
            mode="managed",
            target=object(),
            session_store=store,
        )
        with self.assertRaisesRegex(AgentRuntimeError, "cannot be combined"):
            _runtime_driver(application, langgraph_session_store=Mock())


if __name__ == "__main__":
    unittest.main()
