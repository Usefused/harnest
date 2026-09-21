"""Provider-neutral channel contract: registry, binding validation, and discovery."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

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

    def test_register_create_and_list_round_trip(self):
        register_channel_adapter("stub-test", lambda config: _StubAdapter())
        self.assertIn("stub-test", registered_channel_adapters())
        adapter = create_channel_adapter("stub-test", {})
        self.assertIsInstance(adapter, _StubAdapter)

    def test_duplicate_registration_conflicts(self):
        register_channel_adapter("stub-test", lambda config: _StubAdapter())
        with self.assertRaises(ChannelAdapterConflictError):
            register_channel_adapter("stub-test", lambda config: _StubAdapter())

    def test_unregistered_extension_is_reported(self):
        with self.assertRaises(ChannelAdapterNotFoundError):
            create_channel_adapter("does-not-exist", {})

    def test_adapter_contract_normalizes_offline_and_sends_async(self):
        adapter = _StubAdapter()
        event = adapter.normalize_event(
            {"id": "e1", "installation_id": "team1", "sender": "alice", "conversation": "c1", "text": "hi"}
        )
        self.assertEqual((event.platform, event.sender_id), ("stub", "alice"))
        receipt = AsyncMock(return_value={"ok": True})
        adapter.send_reply = receipt
        import asyncio

        result = asyncio.run(adapter.send_reply(
            ChannelReply(reply_id="r1", platform="stub", installation_id="team1",
                         conversation_id="c1", reply_to={}, content="hi")
        ))
        self.assertEqual(result, {"ok": True})


class ChannelDiscoveryTests(unittest.TestCase):
    def test_discovers_one_binding_per_file_and_rejects_extra_exports(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
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

    def test_missing_directory_yields_no_bindings(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(discover_channel_bindings(Path(temporary) / "channels"), ())

    def test_binding_factory_must_return_channel_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "channels").mkdir()
            (root / "channels" / "bad.py").write_text("def binding():\n    return object()\n")
            with self.assertRaises(ChannelError):
                discover_channel_bindings(root / "channels")
