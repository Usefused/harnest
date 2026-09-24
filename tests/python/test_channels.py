"""Provider-neutral channel contract: registry, binding validation, and discovery."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from harnest.channels import (
    ChannelAdapter,
    ChannelAdapterConflictError,
    ChannelAdapterNotFoundError,
    ChannelBinding,
    ChannelError,
    ChannelEvent,
    ChannelReply,
    _ADAPTER_FACTORIES,
    create_channel_adapter,
    discover_channel_bindings,
    register_channel_adapter,
    registered_channel_adapters,
)


class _StubAdapter(ChannelAdapter):
    platform = "stub"

    def normalize_event(self, raw):
        return ChannelEvent(
            platform="stub", installation_id=raw["installation_id"],
            provider_event_id=raw["id"], kind="message", sender_id=raw["sender"],
            conversation_id=raw["conversation"], message_id=raw["id"],
            occurred_at=0.0, content=raw.get("text", ""),
        )

    async def send_reply(self, reply: ChannelReply):
        return {"ok": True}


class ChannelBindingTests(unittest.TestCase):
    def test_binding_requires_platform_and_extension(self):
        ChannelBinding(platform="slack", extension="fused")
        for platform, extension in (("", "fused"), ("slack", "")):
            with self.assertRaises(ValueError):
                ChannelBinding(platform=platform, extension=extension)


class ChannelAdapterRegistryTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_ADAPTER_FACTORIES.pop, "stub-test", None)

    def test_registry_rejects_missing_and_duplicate_adapters(self):
        with self.assertRaises(ChannelAdapterNotFoundError):
            create_channel_adapter("stub-test", {})
        register_channel_adapter("stub-test", lambda config: _StubAdapter())
        self.assertIn("stub-test", registered_channel_adapters())
        self.assertIsInstance(create_channel_adapter("stub-test", {}), _StubAdapter)
        with self.assertRaises(ChannelAdapterConflictError):
            register_channel_adapter("stub-test", lambda config: _StubAdapter())


class ChannelDiscoveryTests(unittest.TestCase):
    def test_discovers_bindings_when_directory_is_populated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(discover_channel_bindings(root / "channels"), ())
            (root / "channels").mkdir()
            (root / "channels" / "slack.py").write_text(
                "from harnest.channels import ChannelBinding\n"
                "def binding():\n"
                "    return ChannelBinding(platform='slack', extension='fused', "
                "allowed_installations=('T1',))\n"
            )
            bindings = discover_channel_bindings(root / "channels")
            self.assertEqual(len(bindings), 1)
            self.assertEqual(bindings[0].platform, "slack")
            self.assertEqual(bindings[0].allowed_installations, ("T1",))

    def test_binding_factory_must_return_channel_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "channels").mkdir()
            (root / "channels" / "bad.py").write_text("def binding():\n    return object()\n")
            with self.assertRaises(ChannelError):
                discover_channel_bindings(root / "channels")
