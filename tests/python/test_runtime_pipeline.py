from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, AsyncIterator, Mapping, Sequence
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from harnest.application import RuntimeCapabilities
from harnest.context import context
from harnest.credentials import Credential, CredentialProvider
from harnest.lifecycle import LifecycleListener
from harnest.plugin_runtime_driver import PluginRuntimeDriver
from harnest.plugin_runtime_manager import PluginRuntimeManager
from harnest.plugins import ActivatedPlugin, Plugin, PluginContext
from harnest.runtime import _attach_driver_lifecycle
from harnest.runtime_contract import (
    AgentInfo,
    InvocationRequest,
    InvocationResult,
    RuntimeEvent,
    SessionMessage,
    SessionRecord,
)
from harnest.runtime_extensions import ExtensionRuntimeDriver
from harnest.runtime_pipeline import build_runtime_pipeline, start_runtime_pipeline
from harnest.runtime_plugins import RuntimePluginDescriptor
from harnest.runtime_session import StorageRuntimeDriver
from harnest.session import InMemorySessionStore
from harnest.neutral_runtime import create_neutral_app


class _Store(InMemorySessionStore):
    """Record lifecycle events around an ordinary portable session store."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events
        self.ready = False

    async def start(self) -> None:
        self.ready = True
        self.events.append("storage:start")

    async def close(self) -> None:
        self.events.append("storage:close")
        self.ready = False
        await super().close()


class _Credentials(CredentialProvider):
    """Record the private provider stage without exposing secret data."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def start(self) -> None:
        self.events.append("credentials:start")

    async def resolve(self, _request: Any) -> Credential | None:
        return None

    async def close(self) -> None:
        self.events.append("credentials:close")


class _View(PluginContext):
    """One invocation-specific plugin view used to prove fresh binding."""


class _RuntimePlugin(Plugin[_View]):
    """Record application ownership and expose a typed invocation view."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.store: _Store | None = None

    async def start(self, start_context: Any) -> None:
        self.store = start_context.storage("users", _Store)
        self.events.append(f"plugin:start:{self.store.ready}")

    async def stop(self) -> None:
        ready = self.store is not None and self.store.ready
        self.events.append(f"plugin:stop:{ready}")

    def create_context(self, base: PluginContext) -> _View:
        return _View(base.plugin_name)


class _Backend:
    """Minimal deterministic driver whose sessions use the configured store."""

    def __init__(self, events: list[str], store: _Store) -> None:
        self.events = events
        self.store = store
        self.info = AgentInfo(
            id="root",
            name="root",
            description="fixture",
            card={},
            framework="langgraph",
            mode="managed",
        )

    async def create_session(
        self, *, session_id: str, user_id: str, state: Mapping[str, Any]
    ) -> SessionRecord:
        return await self.store.create(
            session_id=session_id, user_id=user_id, state=state
        )

    async def get_session(
        self, *, session_id: str, user_id: str
    ) -> SessionRecord | None:
        return await self.store.get(session_id=session_id, user_id=user_id)

    async def list_sessions(
        self,
        *,
        user_id: str,
        after: str | None = None,
        limit: int | None = None,
    ) -> Sequence[SessionRecord]:
        return await self.store.list(user_id=user_id, after=after, limit=limit)

    async def get_session_messages(
        self, *, session_id: str, user_id: str
    ) -> Sequence[SessionMessage] | None:
        return ()

    async def update_session(
        self,
        *,
        session_id: str,
        user_id: str,
        state_delta: Mapping[str, Any],
    ) -> SessionRecord | None:
        return await self.store.update(
            session_id=session_id,
            user_id=user_id,
            state_delta=state_delta,
        )

    async def delete_session(self, *, session_id: str, user_id: str) -> bool:
        return await self.store.delete(session_id=session_id, user_id=user_id)

    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        self.events.append("backend:invoke")
        return InvocationResult(
            text="ok",
            events=(),
            result="ok",
            session_id=request.session_id,
            metadata={},
        )

    async def stream(
        self, _request: InvocationRequest
    ) -> AsyncIterator[RuntimeEvent]:
        if False:  # pragma: no cover - satisfies the async iterator contract
            yield {}

    async def close(self) -> None:
        self.events.append("backend:close")


def _activated(plugin: _RuntimePlugin) -> ActivatedPlugin:
    """Create one manager-owned descriptor without filesystem activation."""

    name = "pipeline_plugin"
    descriptor = RuntimePluginDescriptor(
        name=name,
        version="1.0.0",
        directory=Path(f"/plugins/{name}"),
        entrypoint="plugin:plugin",
        requires=(),
        capabilities=("context.storage", "lifecycle.agent"),
        digest="sha256:pipeline",
    )
    plugin._bind_identity(name)
    return ActivatedPlugin(
        descriptor, ModuleType(f"harnest.plugins.{name}"), plugin
    )


def _request(invocation_id: str) -> InvocationRequest:
    """Build one stable request against the shared fixture session."""

    return InvocationRequest(
        input="hello",
        user_id="user",
        session_id="session",
        invocation_id=invocation_id,
        metadata={},
        state_delta={},
    )


class RuntimePluginPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_lazy_start_bindings_and_reverse_shutdown_order(self) -> None:
        """Prove one pipeline owns every stage without duplicate acquisition."""

        events: list[str] = []
        views: list[_View] = []
        store = _Store(events)
        credentials = _Credentials(events)
        plugin = _RuntimePlugin(events)
        activated = _activated(plugin)
        manager = PluginRuntimeManager(
            (activated,),
            framework="langgraph",
            root_agent_name="root",
            custom_stores={"users": store},
        )

        @contextmanager
        def resource():
            events.append("resource:start")
            try:
                yield object()
            finally:
                events.append("resource:close")

        def before(_lifecycle: Any, request: InvocationRequest) -> None:
            view = context.plugins("pipeline_plugin", _View)
            self.assertIs(plugin.context, view)
            views.append(view)
            events.append(f"before:{request.invocation_id}")

        listeners = (
            LifecycleListener("resource", resource, 0, "resource.py", 1, "resource"),
            LifecycleListener("before_invoke", before, 0, "before.py", 1, "before"),
        )
        capabilities = RuntimeCapabilities(
            session_store=store,
            custom_stores={"users": store},
            credential_provider=credentials,
        )
        pipeline = build_runtime_pipeline(
            _Backend(events, store),
            capabilities,
            listeners,
            plugin_manager=manager,
        )
        self.assertIsInstance(pipeline, StorageRuntimeDriver)
        self.assertIsInstance(pipeline._driver, PluginRuntimeDriver)
        self.assertIsInstance(pipeline._driver._driver, ExtensionRuntimeDriver)

        try:
            await pipeline.create_session(
                session_id="session", user_id="user", state={}
            )
            await start_runtime_pipeline(pipeline)
            await pipeline.invoke(_request("one"))
            await pipeline.invoke(_request("two"))
            await pipeline.close()
            await pipeline.close()
        finally:
            plugin._clear_identity("pipeline_plugin")

        self.assertEqual(len({id(view) for view in views}), 2)
        self.assertTrue(all(not view.active for view in views))
        self.assertEqual(
            events,
            [
                "storage:start",
                "plugin:start:True",
                "credentials:start",
                "resource:start",
                "before:one",
                "backend:invoke",
                "before:two",
                "backend:invoke",
                "backend:close",
                "resource:close",
                "credentials:close",
                "plugin:stop:True",
                "storage:close",
            ],
        )


class _LifespanDriver:
    """Expose only lifecycle methods needed by empty FastAPI lifespans."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.info = AgentInfo(
            id="root",
            name="root",
            description="fixture",
            card={},
            framework="langgraph",
            mode="managed",
        )

    async def start(self) -> None:
        self.events.append("runtime:start")

    async def close(self) -> None:
        self.events.append("runtime:close")


class RuntimePipelineLifespanTests(unittest.TestCase):
    def test_neutral_http_eagerly_starts_before_accepting_traffic(self) -> None:
        events: list[str] = []
        app = create_neutral_app(
            _LifespanDriver(events), playground_enabled=False
        )

        with TestClient(app):
            self.assertEqual(events, ["runtime:start"])

        self.assertEqual(events, ["runtime:start", "runtime:close"])

    def test_native_adk_lifespan_eagerly_starts_final_pipeline(self) -> None:
        events: list[str] = []

        @asynccontextmanager
        async def adk_lifespan(_app: Any) -> AsyncIterator[None]:
            events.append("adk:start")
            try:
                yield
            finally:
                events.append("adk:stop")

        app = FastAPI(lifespan=adk_lifespan)
        driver = _LifespanDriver(events)
        _attach_driver_lifecycle(app, driver)

        with TestClient(app):
            self.assertEqual(events, ["runtime:start", "adk:start"])

        self.assertEqual(
            events,
            ["runtime:start", "adk:start", "runtime:close", "adk:stop"],
        )


if __name__ == "__main__":
    unittest.main()
