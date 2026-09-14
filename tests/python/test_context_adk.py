import asyncio
from pathlib import Path
from types import ModuleType
import unittest
from types import SimpleNamespace

from harnest import context
from harnest.context import (
    activate_context,
    create_agent_context,
    revoke_context,
)
from harnest.context_adk import adk_agent_context_plugins
from harnest.extension_runtime_manager import ExtensionRuntimeManager
from harnest.extensions import (
    ActivatedExtension,
    Extension,
    ExtensionContext,
    ExtensionContextUnavailableError,
)
from harnest.extension_descriptors import ExtensionDescriptor


def _root_context(*, extension_bindings=None):
    return create_agent_context(
        framework="adk",
        agent_name="root",
        invocation_id="invocation-1",
        user_id="user-1",
        session_id="session-1",
        metadata={},
        resources={"shared": object()},
        extension_bindings=extension_bindings,
    )


def _callback_context():
    native = SimpleNamespace(invocation_id="invocation-1")
    return SimpleNamespace(
        invocation_id="invocation-1", _invocation_context=native
    )


def _native_invocation():
    return SimpleNamespace(
        invocation_id="invocation-1",
        session=SimpleNamespace(user_id="user-1", id="session-1"),
        agent=SimpleNamespace(name="root", root_agent=None),
    )


class _RuntimeContext(ExtensionContext):
    def identity(self):
        """Resolve identity through the invocation rather than retaining it."""

        self._require_active()
        return context.agent_name, context.parent_agent_name, context.depth


class _HarnestExtension(Extension[_RuntimeContext]):
    def create_context(self, base):
        """Expose a typed view while preserving the descriptor identity."""

        return _RuntimeContext(base.extension_name)


def _extension_manager():
    extension = _HarnestExtension()
    extension._bind_identity("temporal")
    descriptor = ExtensionDescriptor(
        name="temporal",
        version="1.0.0",
        directory=Path("/extensions/temporal"),
        entrypoint="extension:extension",
        requires=(),
        capabilities=(),
        digest="sha256:temporal",
    )
    activated = ActivatedExtension(
        descriptor,
        ModuleType("harnest.extensions.temporal"),
        extension,
    )
    manager = ExtensionRuntimeManager(
        (activated,), framework="adk", root_agent_name="root"
    )
    return manager, extension


class ADKAgentContextExtensionTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_run_binds_and_revokes_extension_context(self):
        manager, extension = _extension_manager()
        await manager.start()
        enter, exit_extension = adk_agent_context_plugins("root", manager)
        native = _native_invocation()
        release = asyncio.Event()

        await enter.before_run_callback(invocation_context=native)
        view = context.extensions("temporal", _RuntimeContext)
        self.assertIs(extension.context, view)
        self.assertEqual(view.identity(), ("root", None, 0))
        callback = _callback_context()
        await enter.before_agent_callback(
            agent=SimpleNamespace(name="researcher"),
            callback_context=callback,
        )
        self.assertEqual(view.identity(), ("researcher", "root", 1))
        await exit_extension.after_agent_callback(
            agent=SimpleNamespace(name="researcher"),
            callback_context=callback,
        )

        async def copied_task_result():
            """Prove copied extension authority shares invocation revocation."""

            retained = extension.context
            await release.wait()
            try:
                retained.identity()
            except Exception as error:
                return type(error)
            return None

        copied = asyncio.create_task(copied_task_result())
        await asyncio.sleep(0)
        await exit_extension.after_run_callback(invocation_context=native)
        release.set()

        with self.assertRaises(ExtensionContextUnavailableError):
            _ = extension.context
        self.assertFalse(view.active)
        self.assertIs(await copied, ExtensionContextUnavailableError)
        await manager.close()

    async def test_direct_error_and_cross_task_exit_preserve_owner_cleanup(self):
        manager, extension = _extension_manager()
        await manager.start()
        enter, exit_extension = adk_agent_context_plugins("root", manager)
        native = _native_invocation()
        await enter.before_run_callback(invocation_context=native)
        view = extension.context

        async def wrong_task_exit():
            await exit_extension.after_run_callback(invocation_context=native)

        with self.assertRaisesRegex(RuntimeError, "different task"):
            await asyncio.create_task(wrong_task_exit())
        self.assertTrue(view.active)
        self.assertIs(extension.context, view)

        await exit_extension.on_run_error_callback(
            invocation_context=native,
            error=RuntimeError("private-native-error"),
        )
        self.assertFalse(view.active)
        with self.assertRaises(ExtensionContextUnavailableError):
            _ = extension.context
        await manager.close()

    async def test_direct_adapter_reuses_host_owned_extension_context(self):
        manager, extension = _extension_manager()
        await manager.start()
        active = _root_context(extension_bindings=manager.invocation_bindings())
        enter, exit_extension = adk_agent_context_plugins("root", manager)
        native = _native_invocation()

        with activate_context(active):
            original = extension.context
            await enter.before_run_callback(invocation_context=native)
            await exit_extension.after_run_callback(invocation_context=native)
            self.assertIs(extension.context, original)
            self.assertTrue(original.active)

        revoke_context(active)
        self.assertFalse(original.active)
        await manager.close()

    async def test_child_identity_brackets_all_agent_callbacks(self):
        enter, exit_extension = adk_agent_context_plugins()
        active = _root_context()
        callback = _callback_context()

        with activate_context(active):
            await enter.before_agent_callback(
                agent=SimpleNamespace(name="researcher"),
                callback_context=callback,
            )
            self.assertEqual(context.agent_name, "researcher")
            self.assertEqual(context.parent_agent_name, "root")
            self.assertEqual(context.depth, 1)
            self.assertIs(context.resource("shared"), active._resources["shared"])
            await exit_extension.after_agent_callback(
                agent=SimpleNamespace(name="researcher"),
                callback_context=callback,
            )
            self.assertEqual(context.agent_name, "root")

        revoke_context(active)

    async def test_error_restores_parent_without_replacing_failure(self):
        enter, exit_extension = adk_agent_context_plugins()
        active = _root_context()
        callback = _callback_context()

        with activate_context(active):
            await enter.before_agent_callback(
                agent=SimpleNamespace(name="researcher"),
                callback_context=callback,
            )
            await exit_extension.on_agent_error_callback(
                agent=SimpleNamespace(name="researcher"),
                callback_context=callback,
                error=RuntimeError("private"),
            )
            self.assertEqual(context.agent_name, "root")

        revoke_context(active)

    async def test_parallel_and_reentrant_scopes_restore_their_own_parents(self):
        enter, exit_extension = adk_agent_context_plugins()
        active = _root_context()
        ready = asyncio.Event()
        release = asyncio.Event()

        async def branch(agent_name):
            callback = _callback_context()
            await enter.before_agent_callback(
                agent=SimpleNamespace(name=agent_name),
                callback_context=callback,
            )
            observed = context.agent_name
            ready.set()
            await release.wait()
            await exit_extension.after_agent_callback(
                agent=SimpleNamespace(name=agent_name),
                callback_context=callback,
            )
            return observed, context.agent_name

        with activate_context(active):
            first = asyncio.create_task(branch("researcher"))
            await ready.wait()
            second = asyncio.create_task(branch("writer"))
            await asyncio.sleep(0)
            release.set()
            self.assertEqual(
                await asyncio.gather(first, second),
                [("researcher", "root"), ("writer", "root")],
            )

            outer = _callback_context()
            inner = _callback_context()
            await enter.before_agent_callback(
                agent=SimpleNamespace(name="researcher"),
                callback_context=outer,
            )
            await enter.before_agent_callback(
                agent=SimpleNamespace(name="writer"),
                callback_context=inner,
            )
            self.assertEqual(context.agent_name, "writer")
            await exit_extension.after_agent_callback(
                agent=SimpleNamespace(name="writer"),
                callback_context=inner,
            )
            self.assertEqual(context.agent_name, "researcher")
            await exit_extension.after_agent_callback(
                agent=SimpleNamespace(name="researcher"),
                callback_context=outer,
            )
            self.assertEqual(context.agent_name, "root")

        revoke_context(active)


if __name__ == "__main__":
    unittest.main()
