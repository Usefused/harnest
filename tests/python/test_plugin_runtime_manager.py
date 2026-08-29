from __future__ import annotations

import asyncio
from pathlib import Path
from types import ModuleType
import tempfile
import unittest
from unittest.mock import patch

from harnest.context import (
    ContextResourceError,
    activate_context,
    context,
    create_agent_context,
    derive_agent_context,
    revoke_context,
)
from harnest.plugin_runtime_context import plugin_mutation
from harnest.plugin_runtime_manager import PluginRuntimeError, PluginRuntimeManager
from harnest.plugins import (
    ActivatedPlugin,
    Plugin,
    PluginContext,
    PluginContextUnavailableError,
    activate_runtime_plugins,
)
from harnest.runtime_plugins import RuntimePluginDescriptor, discover_runtime_plugins


class _Store:
    def __init__(self) -> None:
        self.started = True
        self.start_calls = 0

    async def start(self) -> None:
        self.start_calls += 1


class _Context(PluginContext):
    def agent_identity(self) -> tuple[str, str | None, int]:
        """Resolve identity dynamically so managed child scopes remain visible."""

        self._require_active()
        return context.agent_name, context.parent_agent_name, context.depth


class _Plugin(Plugin[_Context]):
    def __init__(
        self,
        name: str,
        events: list[str],
        *,
        fail_start: bool = False,
        fail_stop: bool = False,
        fail_context: bool = False,
    ) -> None:
        self.name = name
        self.events = events
        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.fail_context = fail_context
        self.start_context = None

    async def start(self, context) -> None:
        self.events.append(f"start:{self.name}")
        self.start_context = context
        if self.fail_start:
            raise RuntimeError("private-start-payload")

    async def stop(self) -> None:
        self.events.append(f"stop:{self.name}")
        if self.fail_stop:
            raise RuntimeError("private-stop-payload")

    def create_context(self, base: PluginContext) -> _Context:
        if self.fail_context:
            raise RuntimeError("private-context-payload")
        return _Context(base.plugin_name)


def _activated(
    name: str,
    plugin: Plugin,
    *,
    requires: tuple[str, ...] = (),
    capabilities: tuple[str, ...] = (),
) -> ActivatedPlugin:
    descriptor = RuntimePluginDescriptor(
        name=name,
        version="1.0.0",
        directory=Path(f"/plugins/{name}"),
        entrypoint="plugin:plugin",
        requires=requires,
        capabilities=capabilities,
        digest=f"sha256:{name}",
    )
    plugin._bind_identity(name)
    return ActivatedPlugin(descriptor, ModuleType(f"harnest.plugins.{name}"), plugin)


def _manager(
    activated: tuple[ActivatedPlugin, ...],
    *,
    stores: dict[str, object] | None = None,
    release_namespaces_on_close: bool = False,
) -> PluginRuntimeManager:
    return PluginRuntimeManager(
        activated,
        framework="langgraph",
        root_agent_name="support",
        custom_stores=stores,
        release_namespaces_on_close=release_namespaces_on_close,
    )


def _agent(bindings, *, invocation_id: str = "invoke-1"):
    return create_agent_context(
        framework="langgraph",
        agent_name="support",
        invocation_id=invocation_id,
        user_id="user-1",
        session_id="session-1",
        metadata={},
        resources={},
        plugin_bindings=bindings,
    )


class PluginRuntimeManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_releases_transferred_namespace_exactly_once(self):
        events: list[str] = []
        activated = (_activated("temporal", _Plugin("temporal", events)),)
        manager = _manager(activated, release_namespaces_on_close=True)

        with patch("harnest.plugins.release_runtime_plugins") as release:
            await manager.close()
            await manager.close()

        release.assert_called_once_with((activated[0].descriptor,))
        self.assertEqual(events, [])

    async def test_namespace_cleanup_does_not_replace_stop_failure(self):
        events: list[str] = []
        activated = (
            _activated("temporal", _Plugin("temporal", events, fail_stop=True)),
        )
        manager = _manager(activated, release_namespaces_on_close=True)
        await manager.start()

        with (
            patch(
                "harnest.plugins.release_runtime_plugins",
                side_effect=RuntimeError("private-namespace-payload"),
            ) as release,
            self.assertRaises(PluginRuntimeError) as captured,
        ):
            await manager.close()

        release.assert_called_once()
        self.assertIn("stop failed", str(captured.exception))
        self.assertNotIn("private-namespace-payload", str(captured.exception))
        notes = getattr(captured.exception, "__notes__", ())
        self.assertTrue(any("plugin namespace cleanup" in note for note in notes))

    async def test_failed_start_releases_transferred_namespace_on_close(self):
        events: list[str] = []
        activated = (
            _activated("temporal", _Plugin("temporal", events, fail_start=True)),
        )
        manager = _manager(activated, release_namespaces_on_close=True)
        with self.assertRaises(PluginRuntimeError):
            await manager.start()

        with patch("harnest.plugins.release_runtime_plugins") as release:
            await manager.close()
            await manager.close()

        release.assert_called_once_with((activated[0].descriptor,))

    async def test_discovered_namespace_and_manager_share_one_plugin_singleton(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugins"
            directory = root / "temporal"
            directory.mkdir(parents=True)
            (directory / "plugin.yaml").write_text(
                """apiVersion: harnest.dev/v1alpha1
kind: RuntimePlugin
metadata:
  name: temporal
  version: 1.0.0
runtime:
  entrypoint: plugin:plugin
""",
                encoding="utf-8",
            )
            (directory / "plugin.py").write_text(
                """from harnest.plugins import Plugin, PluginContext

class TemporalContext(PluginContext):
    def ready(self):
        self._require_active()
        return "ready"

class TemporalPlugin(Plugin[TemporalContext]):
    def create_context(self, base):
        return TemporalContext(base.plugin_name)

plugin = TemporalPlugin()
""",
                encoding="utf-8",
            )
            descriptors = discover_runtime_plugins(root)
            activated = activate_runtime_plugins(descriptors)
            manager = _manager(activated, release_namespaces_on_close=True)
            active = None
            import harnest.plugins as namespace

            try:
                await manager.start()
                active = _agent(manager.invocation_bindings())
                with activate_context(active):
                    view = context.plugins("temporal")
                    self.assertIs(namespace.temporal.plugin.context, view)
                    self.assertEqual(view.ready(), "ready")
            finally:
                if active is not None:
                    revoke_context(active)
                await manager.close()
            self.assertFalse(hasattr(namespace, "temporal"))

    async def test_manager_rejects_falsey_non_mapping_storage(self):
        with self.assertRaisesRegex(TypeError, "custom storage must be a mapping"):
            PluginRuntimeManager(
                (),
                framework="adk",
                root_agent_name="support",
                custom_stores=[],
            )

    async def test_manager_rejects_missing_and_cyclic_dependencies_before_start(self):
        events: list[str] = []
        missing = _activated(
            "worker", _Plugin("worker", events), requires=("core",)
        )
        with self.assertRaisesRegex(ValueError, "missing plugin 'core'"):
            _manager((missing,))

        left = _activated("left", _Plugin("left", events), requires=("right",))
        right = _activated("right", _Plugin("right", events), requires=("left",))
        with self.assertRaisesRegex(ValueError, "dependency cycle"):
            _manager((left, right))
        self.assertEqual(events, [])

    async def test_start_is_concurrent_safe_and_close_reverses_dependencies(self):
        events: list[str] = []
        store = _Store()
        core = _Plugin("core", events)
        worker = _Plugin("worker", events)
        manager = _manager(
            (
                _activated("worker", worker, requires=("core",)),
                _activated(
                    "core", core, capabilities=("context.storage",)
                ),
            ),
            stores={"state-db": store},
        )

        await asyncio.gather(manager.start(), manager.start(), manager.start())

        self.assertEqual(events, ["start:core", "start:worker"])
        self.assertTrue(manager.started)
        self.assertIs(core.start_context.storage("state-db", _Store), store)
        self.assertEqual(store.start_calls, 0)
        self.assertEqual(core.start_context.plugin_name, "core")
        self.assertEqual(core.start_context.framework, "langgraph")
        self.assertEqual(core.start_context.root_agent_name, "support")
        self.assertNotIn("state-db", repr(core.start_context))
        for forbidden in (
            "credentials",
            "session",
            "checkpoints",
            "assets",
            "principal",
            "resources",
        ):
            self.assertFalse(hasattr(core.start_context, forbidden))

        await manager.close()
        await manager.close()
        self.assertEqual(
            events,
            ["start:core", "start:worker", "stop:worker", "stop:core"],
        )

    async def test_concurrent_managers_cannot_start_or_stop_same_singleton(self):
        """Keep compilation refs independent from exclusive runtime ownership."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugins"
            directory = root / "temporal"
            directory.mkdir(parents=True)
            (directory / "plugin.yaml").write_text(
                """apiVersion: harnest.dev/v1alpha1
kind: RuntimePlugin
metadata:
  name: temporal
  version: 1.0.0
runtime:
  entrypoint: plugin:plugin
""",
                encoding="utf-8",
            )
            (directory / "plugin.py").write_text(
                """import asyncio
from harnest.plugins import Plugin

class TemporalPlugin(Plugin):
    def __init__(self):
        self.start_calls = 0
        self.stop_calls = 0
        self.start_entered = asyncio.Event()
        self.release_start = asyncio.Event()

    async def start(self, context):
        del context
        self.start_calls += 1
        self.start_entered.set()
        await self.release_start.wait()

    async def stop(self):
        self.stop_calls += 1

plugin = TemporalPlugin()
""",
                encoding="utf-8",
            )
            descriptors = discover_runtime_plugins(root)
            first = activate_runtime_plugins(descriptors)
            second = activate_runtime_plugins(descriptors)
            first_manager = _manager(
                first, release_namespaces_on_close=True
            )
            second_manager = _manager(
                second, release_namespaces_on_close=True
            )
            plugin = first[0].plugin
            first_start = asyncio.create_task(first_manager.start())
            import harnest.plugins as namespace

            try:
                await asyncio.wait_for(plugin.start_entered.wait(), timeout=1)
                with self.assertRaisesRegex(
                    PluginRuntimeError, "already active in another runtime"
                ):
                    await second_manager.start()
                self.assertEqual(plugin.start_calls, 1)

                plugin.release_start.set()
                await first_start
                await second_manager.close()

                # The rejected runtime releases only its compiler acquisition;
                # the active owner keeps both its singleton and namespace live.
                self.assertEqual(plugin.stop_calls, 0)
                self.assertTrue(first_manager.started)
                self.assertTrue(hasattr(namespace, "temporal"))

                await first_manager.close()
                self.assertEqual(plugin.stop_calls, 1)
                self.assertFalse(hasattr(namespace, "temporal"))
            finally:
                plugin.release_start.set()
                await asyncio.gather(first_start, return_exceptions=True)
                await second_manager.close()
                await first_manager.close()

    async def test_start_storage_requires_declared_context_capability(self):
        events: list[str] = []
        store = _Store()
        permitted = _Plugin("permitted", events)
        restricted = _Plugin("restricted", events)
        manager = _manager(
            (
                _activated(
                    "permitted",
                    permitted,
                    capabilities=("context.storage",),
                ),
                _activated("restricted", restricted),
            ),
            stores={"state-db": store},
        )

        await manager.start()

        self.assertIs(permitted.start_context.storage("state-db"), store)
        with self.assertRaisesRegex(ContextResourceError, "not available"):
            restricted.start_context.storage("state-db")
        await manager.close()

    async def test_partial_start_failure_unwinds_and_detaches_authored_error(self):
        events: list[str] = []
        core = _Plugin("core", events)
        worker = _Plugin("worker", events, fail_start=True)
        manager = _manager(
            (
                _activated("core", core),
                _activated("worker", worker, requires=("core",)),
            )
        )

        with self.assertRaises(PluginRuntimeError) as captured:
            await manager.start()

        self.assertEqual(
            events, ["start:core", "start:worker", "stop:worker", "stop:core"]
        )
        self.assertNotIn("private-start-payload", str(captured.exception))
        self.assertIsNone(captured.exception.__context__)
        await manager.close()
        self.assertEqual(events.count("stop:core"), 1)

    async def test_cleanup_attempts_every_plugin_and_sanitizes_failure(self):
        events: list[str] = []
        core = _Plugin("core", events, fail_stop=True)
        worker = _Plugin("worker", events, fail_stop=True)
        manager = _manager(
            (
                _activated("core", core),
                _activated("worker", worker, requires=("core",)),
            )
        )
        await manager.start()

        with self.assertRaises(PluginRuntimeError) as captured:
            await manager.close()

        self.assertEqual(events[-2:], ["stop:worker", "stop:core"])
        self.assertNotIn("private-stop-payload", str(captured.exception))
        self.assertIsNone(captured.exception.__context__)

    async def test_invocation_views_bind_singleton_and_follow_child_identity(self):
        events: list[str] = []
        plugin = _Plugin("temporal", events)
        manager = _manager((_activated("temporal", plugin),))
        await manager.start()
        active = _agent(manager.invocation_bindings())

        with activate_context(active):
            view = context.plugins("temporal", _Context)
            self.assertIs(plugin.context, view)
            self.assertEqual(view.agent_identity(), ("support", None, 0))
            child = derive_agent_context(active, agent_name="researcher")
            with activate_context(child):
                self.assertIs(context.plugins("temporal"), view)
                self.assertEqual(
                    plugin.context.agent_identity(), ("researcher", "support", 1)
                )
            with self.assertRaisesRegex(ContextResourceError, "not available"):
                context.plugins("missing")
            with self.assertRaisesRegex(ContextResourceError, "must expose"):
                context.plugins("temporal", dict)

        with self.assertRaises(PluginContextUnavailableError):
            _ = plugin.context
        revoke_context(active)
        with self.assertRaises(PluginContextUnavailableError):
            view.agent_identity()
        await manager.close()

    async def test_copied_task_and_previous_invocation_views_are_revoked(self):
        events: list[str] = []
        plugin = _Plugin("temporal", events)
        manager = _manager((_activated("temporal", plugin),))
        await manager.start()
        first = _agent(manager.invocation_bindings())
        ready = asyncio.Event()
        release = asyncio.Event()

        async def retained_child():
            view = plugin.context
            ready.set()
            await release.wait()
            return view.agent_identity()

        with activate_context(first):
            old = context.plugins("temporal")
            child = asyncio.create_task(retained_child())
            await ready.wait()
        revoke_context(first)
        release.set()
        with self.assertRaises(PluginContextUnavailableError):
            await child

        second = _agent(manager.invocation_bindings(), invocation_id="invoke-2")
        with activate_context(second):
            current = context.plugins("temporal")
            self.assertIsNot(current, old)
            with self.assertRaises(PluginContextUnavailableError):
                old._require_active()
        revoke_context(second)
        await manager.close()

    async def test_context_factory_failure_revokes_earlier_views_and_is_sanitized(self):
        events: list[str] = []
        core = _Plugin("core", events)
        worker = _Plugin("worker", events, fail_context=True)
        manager = _manager(
            (
                _activated("core", core),
                _activated("worker", worker, requires=("core",)),
            )
        )
        await manager.start()

        with self.assertRaises(PluginRuntimeError) as captured:
            manager.invocation_bindings()

        self.assertNotIn("private-context-payload", str(captured.exception))
        self.assertIsNone(captured.exception.__context__)
        await manager.close()


class PluginMutationAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_audit_records_commit_and_failure_without_payloads(self):
        with self.assertLogs("harnest.agent.plugin.audit", level="INFO") as logs:
            async with plugin_mutation(
                "temporal", "workflow.start", trigger="agent"
            ):
                pass
            with self.assertRaisesRegex(RuntimeError, "customer-secret"):
                async with plugin_mutation(
                    "temporal", "workflow.cancel", trigger="user"
                ):
                    raise RuntimeError("customer-secret")

        self.assertEqual(
            [record.outcome for record in logs.records], ["committed", "failed"]
        )
        self.assertEqual(
            [record.trigger for record in logs.records], ["agent", "user"]
        )
        rendered = " ".join(record.getMessage() for record in logs.records)
        self.assertNotIn("customer-secret", rendered)
        self.assertFalse(any(hasattr(record, "user_id") for record in logs.records))


if __name__ == "__main__":
    unittest.main()
