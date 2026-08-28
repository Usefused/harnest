import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from harnest.application import CompiledApplication
from harnest.checkpoint import ADKStore
from harnest.neutral_runtime import AgentInfo, SessionRecord
from harnest.runtime import AgentRuntimeError, _runtime_driver
from harnest.runtime_session import StorageRuntimeDriver
from harnest.session import InMemorySessionStore


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

        with self.assertRaisesRegex(RuntimeError, "backend failed"):
            await driver.close()
        self.assertEqual(store.closed, 1)


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
        self.assertIs(selected._driver, backend)
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
