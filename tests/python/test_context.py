import unittest

from harnest.assets import (
    AssetNotFoundError,
    AssetScope,
    AssetStoreError,
    MemoryAssetStore,
)
from harnest.content import AssetRef
from harnest.context import (
    ContextResourceError,
    ContextUnavailableError,
    activate_context,
    activate_agent_scope,
    bind_resource,
    context,
    create_agent_context,
    derive_agent_context,
    registration_for,
)
from harnest.client_tool import client_tool
from harnest.lifecycle import lifecycle
from harnest.tool import tool


async def _chunks(value: bytes):
    yield value


class ContextAuthoringTests(unittest.TestCase):
    def test_decorator_records_one_valid_provider_name(self):
        @context("memory", order=7)
        def memory():
            return object()

        registration = registration_for(memory)

        self.assertEqual(registration.name, "memory")
        self.assertEqual(registration.order, 7)
        with self.assertRaisesRegex(TypeError, "only one"):
            context("other")(memory)
        with self.assertRaisesRegex(ValueError, "Python identifier"):
            context("not-valid")

    def test_tools_and_context_providers_are_mutually_exclusive(self):
        def documented():
            """A documented callable."""

        for decorate in (
            lambda: tool(context("memory")(documented)),
            lambda: context("memory")(tool(documented)),
        ):
            with self.subTest(order=decorate), self.assertRaisesRegex(
                TypeError, "context|tool"
            ):
                decorate()

    def test_tools_and_lifecycle_extensions_are_mutually_exclusive(self):
        decorators = (tool, client_tool)
        for tool_decorator in decorators:
            for lifecycle_first in (True, False):
                def documented():
                    """A documented callable."""

                def decorate():
                    if lifecycle_first:
                        return tool_decorator(lifecycle.before_invoke(documented))
                    return lifecycle.before_invoke(tool_decorator(documented))

                with self.subTest(
                    tool=tool_decorator.__name__, lifecycle_first=lifecycle_first
                ), self.assertRaisesRegex(TypeError, "lifecycle|tool"):
                    decorate()

    def test_context_is_task_scoped_typed_and_unavailable_outside_execution(self):
        active = create_agent_context(
            framework="langgraph",
            agent_name="support",
            invocation_id="run-1",
            user_id="user-1",
            session_id="session-1",
            metadata={"channel": "web"},
            resources={"memory": []},
        )

        with self.assertRaises(ContextUnavailableError):
            context.current()
        with activate_context(active):
            self.assertEqual(context.user_id, "user-1")
            self.assertEqual(context.metadata, {"channel": "web"})
            self.assertIsInstance(context.resource("memory", list), list)
            bind_resource(active, "cache", {})
            self.assertEqual(context.resource("cache"), {})
            with self.assertRaisesRegex(ContextResourceError, "must be dict"):
                context.resource("memory", dict)
            with self.assertRaisesRegex(ContextResourceError, "not available"):
                context.resource("missing")
        with self.assertRaises(ContextUnavailableError):
            context.resource("memory")

    def test_duplicate_binding_cannot_replace_an_existing_capability(self):
        active = create_agent_context(
            framework="adk",
            agent_name="support",
            invocation_id="run-1",
            user_id="user-1",
            session_id="session-1",
            metadata={},
            resources={"memory": object()},
        )

        with self.assertRaisesRegex(ValueError, "already bound"):
            bind_resource(active, "memory", object())

    def test_subagent_context_derives_identity_without_copying_authority(self):
        """Represent child execution as a scoped view rather than a new context type."""

        active = create_agent_context(
            framework="adk",
            agent_name="root",
            invocation_id="run-1",
            user_id="user-1",
            session_id="session-1",
            metadata={},
            resources={"memory": object()},
        )
        child = derive_agent_context(active, agent_name="researcher")

        with activate_context(child):
            self.assertEqual(context.agent_name, "researcher")
            self.assertEqual(context.parent_agent_name, "root")
            self.assertEqual(context.depth, 1)
            self.assertFalse(context.is_root)
            self.assertIs(context.resource("memory"), active.resource("memory"))

    def test_agent_scope_narrows_and_restores_the_active_identity(self):
        """Let framework callbacks identify a child without owning its authority."""

        active = create_agent_context(
            framework="adk",
            agent_name="root",
            invocation_id="run-1",
            user_id="user-1",
            session_id="session-1",
            metadata={},
            resources={"memory": object()},
        )

        with activate_context(active):
            with activate_agent_scope("researcher") as child:
                self.assertIsNotNone(child)
                self.assertEqual(context.agent_name, "researcher")
                self.assertEqual(context.parent_agent_name, "root")
                self.assertEqual(context.depth, 1)
                self.assertIs(context.resource("memory"), active.resource("memory"))
            self.assertEqual(context.agent_name, "root")
            self.assertTrue(context.is_root)


class ContextAssetsTests(unittest.IsolatedAsyncioTestCase):
    async def test_named_assets_save_and_route_opaque_references(self):
        default = MemoryAssetStore()
        generated = MemoryAssetStore()
        active = create_agent_context(
            framework="langgraph",
            agent_name="artist",
            invocation_id="run-assets",
            user_id="user-1",
            session_id="session-1",
            metadata={},
            resources={},
            asset_stores={"default": default, "generated": generated},
        )

        with activate_context(active), self.assertLogs(
            "harnest.agent.asset.audit", level="INFO"
        ) as captured:
            reference = await context.assets("generated").save(
                media_type="image/png",
                chunks=_chunks(b"pixels"),
                label=" profile-preview ",
            )
            default_audio = await context.assets.save(
                media_type="audio/mpeg",
                chunks=_chunks(b"sound"),
            )
            self.assertEqual(
                await context.assets("generated").get(reference), b"pixels"
            )
            self.assertEqual(await context.assets.get(default_audio), b"sound")

        self.assertIsInstance(reference, AssetRef)
        self.assertEqual(reference.store, "generated")
        self.assertEqual(reference.label, "profile-preview")
        self.assertNotIn("profile-preview", repr(reference))
        self.assertEqual(default_audio.store, "default")
        self.assertEqual(captured.records[0].operation, "save")
        self.assertEqual(captured.records[0].outcome, "committed")
        self.assertFalse(hasattr(reference, "storage"))

    async def test_named_asset_context_rejects_store_confusion_and_missing_default(self):
        generated = MemoryAssetStore()
        active = create_agent_context(
            framework="adk",
            agent_name="support",
            invocation_id="run-assets",
            user_id="user-1",
            session_id="session-1",
            metadata={},
            resources={},
            asset_stores={"generated": generated},
        )

        with activate_context(active):
            reference = await context.assets("generated").save(
                media_type="application/pdf",
                chunks=_chunks(b"document"),
            )
            with self.assertRaisesRegex(AssetNotFoundError, "unavailable"):
                await context.assets.save(
                    media_type="application/pdf",
                    chunks=_chunks(b"other"),
                )
            with self.assertRaisesRegex(AssetStoreError, "wrong storage"):
                await context.assets("generated").get(
                    AssetRef(assetId=reference.asset_id, store="default")
                )

    async def test_retained_named_asset_context_is_revoked(self):
        storage = MemoryAssetStore()
        active = create_agent_context(
            framework="adk",
            agent_name="support",
            invocation_id="run-assets",
            user_id="user-1",
            session_id="session-1",
            metadata={},
            resources={},
            asset_stores={"default": storage},
        )

        with activate_context(active):
            retained = context.assets
            reference = await retained.save(
                media_type="text/plain",
                chunks=_chunks(b"notes"),
            )

        with self.assertRaises(ContextUnavailableError):
            await retained.get(reference)

    async def test_assets_route_by_reference_and_apply_invocation_scope(self):
        default = MemoryAssetStore()
        media = MemoryAssetStore()
        scope = AssetScope(user_id="user-1", session_id="session-1")
        record = await media.save(
            scope=scope,
            media_type="image/jpeg",
            chunks=_chunks(b"pixels"),
        )
        active = create_agent_context(
            framework="adk",
            agent_name="support",
            invocation_id="run-1",
            user_id="user-1",
            session_id="session-1",
            metadata={},
            resources={},
            asset_stores={"default": default, "media": media},
        )

        class Reference:
            asset_id = record.asset_id
            store = "media"

        with activate_context(active):
            self.assertEqual(await context.assets.get(Reference()), b"pixels")
            self.assertEqual((await context.assets.stat(Reference())).size_bytes, 6)
            with self.assertRaises(AssetNotFoundError):
                await context.assets.get(record.asset_id)

    async def test_assets_do_not_cross_session_scope(self):
        media = MemoryAssetStore()
        record = await media.save(
            scope=AssetScope(user_id="user-1", session_id="other"),
            media_type="image/jpeg",
            chunks=_chunks(b"pixels"),
        )
        active = create_agent_context(
            framework="langgraph",
            agent_name="support",
            invocation_id="run-1",
            user_id="user-1",
            session_id="session-1",
            metadata={},
            resources={},
            asset_stores={"media": media},
        )
        with activate_context(active), self.assertRaises(AssetNotFoundError):
            await context.assets.get(record.asset_id, store="media")


if __name__ == "__main__":
    unittest.main()
