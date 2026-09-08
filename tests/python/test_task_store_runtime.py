"""Exercise custom storage through compiler, managed runtime and durable workers."""

from dataclasses import replace
import asyncio
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from harnest import context, cron
from harnest.application import CompiledApplication
from harnest.bundle import compile_artifact
from harnest.context import activate_context, create_agent_context, revoke_context
from harnest.extension_loader import discover_extensions
from harnest.runtime import _runtime_driver, _task_runtime_manager
from harnest.runtime_task import TaskRuntimeDriver, TaskRuntimeError
from harnest.runtime_contract import InvocationRequest
from harnest.runtime_task_store import ProviderTaskRuntimeManager
from harnest.storage_registry import StorageRegistry
from harnest.task import task, CompiledTask, MemoryTaskStore, registration_for

from test_task_runtime import _CronCallingDriver


def _cron_request():
    """Build a user-owned request for the fake framework tool invocation."""

    return InvocationRequest(input="schedule it", user_id="user-1", session_id="session-1", invocation_id="inv-cron-tool", metadata={}, state_delta={})


def application_for(function, store):
    """Compile the same authored callable against any structural task provider."""

    authored = task(function)
    compiled = CompiledTask(
        name="harnest.provider_test.tasks.deliver", source="tasks/deliver.py",
        definition=registration_for(authored), authored=authored,
    )
    application = CompiledApplication(
        name="provider_test", framework="langgraph", mode="managed", target=object(),
        tasks=(compiled,), task_store=store, cron_store=store,
    )
    return authored, application


def invocation(owner="user-1"):
    """Create a revocable user scope independently of a model provider."""

    return create_agent_context(
        framework="langgraph", agent_name="provider_test", invocation_id="invocation",
        user_id=owner, session_id=f"session-{owner}", metadata={}, resources={},
    )


class CustomProvider:
    """A user adapter needs the protocol methods, not inheritance from built-ins."""

    def __init__(self):
        self.implementation = MemoryTaskStore()
        for name in dir(MemoryTaskStore):
            if not name.startswith("_"):
                setattr(self, name, getattr(self.implementation, name))


class ProviderRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_pipeline_owns_shared_provider_once_before_workers(self):
        """The production selector starts storage before claims and closes it once."""

        events = []

        class OwnedStore(MemoryTaskStore):
            async def start(self):
                """Observe the real outer storage lifecycle entering ownership."""
                events.append("start")

            async def close(self):
                """Observe release after workers stop."""
                events.append("close")

            async def claim_tasks(self, **options):
                """Fail if worker claims escape the provider's owned lifetime."""
                if events != ["start"]:
                    raise AssertionError("claim outside storage lifetime")
                return await super().claim_tasks(**options)

        async def deliver(value):
            """Execute without a framework or model dependency."""
            return value

        authored, application = application_for(deliver, OwnedStore())
        with patch("harnest.runtime._langgraph_runtime_driver", return_value=_CronCallingDriver(authored)):
            driver = _runtime_driver(application)
        try:
            await driver.start()
            await driver.start()
            self.assertEqual(events, ["start"])
            handle = await authored.defer(value="pipeline")
            for _ in range(100):
                if await handle.status() == "succeeded":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(await handle.result(), "pipeline")
        finally:
            await driver.close()
            await driver.close()
        self.assertEqual(events, ["start", "close"])

    async def test_custom_provider_runs_and_scrubs_private_payload_without_procrastinate(self):
        """Execute an authored task and persist its result via structural storage."""

        observed = []
        completed = asyncio.Event()

        async def deliver(value):
            """Capture the owner's restored identity and signal completion."""
            observed.append((value, context.user_id))
            completed.set()
            return {"delivered": True}

        store = CustomProvider()
        authored, application = application_for(deliver, store)
        manager = ProviderTaskRuntimeManager(application)
        active = invocation()
        try:
            with patch("harnest.runtime_task._load_procrastinate", side_effect=AssertionError("legacy backend loaded")):
                await manager.start()
                with activate_context(active):
                    handle = await authored.defer(value="private", idempotency_key="once")
                    repeated = await authored.defer(value="private", idempotency_key="once")
                self.assertEqual(handle.id, repeated.id)
                await asyncio.wait_for(completed.wait(), 2)
                for _ in range(100):
                    if await handle.status() == "succeeded":
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(await handle.result(), {"delivered": True})
                saved = await store.get_task(application_id=application.name, job_id=handle.id)
                self.assertEqual(saved.arguments, {})
                self.assertIsNone(saved.invocation)
                self.assertEqual(observed, [("private", "user-1")])
        finally:
            revoke_context(active)
            await manager.close()

    async def test_cron_survives_runtime_restart_and_cancellation_prevents_dispatch(self):
        """Persist an occurrence cursor and drain it through a fresh worker owner."""

        completed = asyncio.Event()
        observed = []

        async def deliver(value):
            """Record scheduled work under its restored owner scope."""
            observed.append((value, context.user_id))
            completed.set()

        store = MemoryTaskStore()
        authored, application = application_for(deliver, store)
        first = ProviderTaskRuntimeManager(application)
        active = invocation()
        try:
            await first.start()
            with activate_context(active), cron._activate_runtime(first.cron_runtime):
                job = await cron.create(key="report", expression="* * * * *", task=authored, arguments={"value": "report"})
            await first.close()
            record = await store.get_cron(application_id=application.name, user_id="user-1", schedule_id=job.id)
            await store.update_cron(replace(record, next_run_at=time.time()-1), expected_revision=record.revision)
            second = ProviderTaskRuntimeManager(application)
            try:
                await second.start()
                await asyncio.wait_for(completed.wait(), 2)
                with activate_context(active), cron._activate_runtime(second.cron_runtime):
                    self.assertEqual((await cron.get(job.id)).status, "active")
                    await cron.cancel(job.id)
                await second.cron_runtime.dispatch(time.time()+120)
                self.assertEqual(observed, [("report", "user-1")])
            finally:
                await second.close()
        finally:
            revoke_context(active)
            await first.close()

    async def test_other_owner_cannot_read_or_cancel_retained_handle(self):
        """Retained handles cannot bypass active invocation owner filtering."""

        async def deliver(value):
            """Return the queued value."""
            return value

        store = MemoryTaskStore()
        authored, application = application_for(deliver, store)
        manager = ProviderTaskRuntimeManager(application)
        first, second = invocation(), invocation("user-2")
        try:
            await manager.start()
            with activate_context(first):
                handle = await authored.defer(value="private", schedule_in=60)
            with activate_context(second):
                with self.assertRaises(TaskRuntimeError):
                    await handle.status()
                with self.assertRaises(TaskRuntimeError):
                    await handle.cancel()
            with activate_context(first):
                self.assertTrue(await handle.cancel())
        finally:
            revoke_context(first)
            revoke_context(second)
            await manager.close()

    async def test_tool_and_stream_use_explicit_provider(self):
        """Keep cron available during managed invoke and stream execution."""

        async def deliver(value):
            """Provide one schedulable compiler-discovered target."""

        store = MemoryTaskStore()
        authored, application = application_for(deliver, store)
        manager = _task_runtime_manager(application)
        self.assertIsInstance(manager, ProviderTaskRuntimeManager)
        driver = TaskRuntimeDriver(_CronCallingDriver(authored), manager)
        try:
            response = await driver.invoke(_cron_request())
            self.assertEqual(response.result.status, "active")
            events = [event async for event in driver.stream(_cron_request())]
            self.assertTrue(events)
        finally:
            await driver.close()


class ProviderCompilerTests(unittest.TestCase):
    def test_factory_is_shared_and_artifact_does_not_require_procrastinate(self):
        """Discover task/cron lifecycle roles and compile their real manifest."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lifecycle = root / "lifecycle"
            lifecycle.mkdir()
            (lifecycle / "storage.py").write_text(
                "from harnest import lifecycle\n"
                "from harnest.checkpoint import MemoryStore\n"
                "from harnest.task import MemoryTaskStore\n"
                "@lifecycle.storage.sessions\n@lifecycle.storage.checkpoints\n"
                "def sessions():\n    return MemoryStore()\n"
                "@lifecycle.storage.tasks\n@lifecycle.storage.cron\n"
                "def work():\n    return MemoryTaskStore()\n"
            )
            discovered = discover_extensions(lifecycle, framework="langgraph")
            self.assertIs(discovered.storage_registry.tasks, discovered.storage_registry.cron)
            self.assertEqual(len(discovered.storage_registry.owned_resources()), 2)

            async def deliver():
                """Compile one task using the custom storage provider."""

            _, application = application_for(deliver, discovered.storage_registry.tasks)
            (root / "agent.py").write_text("root_agent = object()\n")
            with patch("harnest.bundle.compile_application", return_value=application):
                manifest = compile_artifact(root, root / ".harnest" / "artifact", framework="langgraph")
            self.assertEqual(manifest["runtimeDependencies"], [])

    def test_split_task_and_cron_providers_are_rejected(self):
        """Atomic occurrence handoff requires the same provider transaction boundary."""

        with self.assertRaisesRegex(ValueError, "share one provider"):
            StorageRegistry(tasks=MemoryTaskStore(), cron=MemoryTaskStore())
