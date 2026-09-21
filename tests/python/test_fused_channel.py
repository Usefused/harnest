"""The generic Fused-backed channel adapter: any connected service, one class."""

from __future__ import annotations

from contextlib import asynccontextmanager
import unittest
from unittest.mock import AsyncMock, patch

from harnest.channel_storage import ChannelReply
from harnest.channels import ChannelError, create_channel_adapter, registered_channel_adapters
import harnest_fused  # noqa: F401 (import registers the "fused" channel adapter)
from harnest_fused.channel import FusedChannelAdapter, FusedChannelConfig, FusedEventMapping
from harnest_fused.client import FusedMCPClient


def _mapping(**overrides):
    fields = dict(
        provider_event_id="event.id", kind="event.type", sender_id="event.user",
        conversation_id="event.channel", message_id="event.id",
        occurred_at="event.ts", content="event.text", thread_id="event.thread_ts",
    )
    fields.update(overrides)
    return FusedEventMapping(**fields)


def _config(**overrides):
    defaults = dict(
        platform="slack", installation_id="T1", mapping=_mapping(),
        client=FusedMCPClient.streamable_http("https://engine.test/mcp"),
        reply_operation="postMessage",
        reply_arguments={"channel": "$conversation_id", "text": "$content", "kind": "reply"},
    )
    defaults.update(overrides)
    return FusedChannelConfig(**defaults)


class FusedChannelRegistrationTests(unittest.TestCase):
    def test_importing_harnest_fused_registers_the_fused_extension(self):
        self.assertIn("fused", registered_channel_adapters())

    def test_create_channel_adapter_builds_from_plain_data(self):
        adapter = create_channel_adapter("fused", {
            "platform": "slack", "installation_id": "T1", "url": "https://engine.test/mcp",
            "reply_operation": "postMessage",
            "mapping": {
                "provider_event_id": "event.id", "kind": "event.type", "sender_id": "event.user",
                "conversation_id": "event.channel", "message_id": "event.id", "occurred_at": "event.ts",
            },
        })
        self.assertIsInstance(adapter, FusedChannelAdapter)
        self.assertEqual(adapter.platform, "slack")


class FusedEventMappingTests(unittest.TestCase):
    def test_resolves_dotted_paths_for_any_service_shape(self):
        mapping = _mapping()
        raw = {"event": {"id": "1", "type": "message", "user": "U1", "channel": "C1",
                          "ts": "1700000000.1", "text": "hi", "thread_ts": "1700000000.0"}}
        event = mapping.apply(platform="slack", installation_id="T1", raw=raw)
        self.assertEqual((event.platform, event.installation_id, event.sender_id), ("slack", "T1", "U1"))
        self.assertEqual((event.conversation_id, event.content, event.thread_id), ("C1", "hi", "1700000000.0"))
        self.assertEqual(event.occurred_at, 1700000000.1)

    def test_missing_required_field_raises_channel_error(self):
        with self.assertRaises(ChannelError):
            _mapping().apply(platform="slack", installation_id="T1", raw={"event": {"id": "1"}})

    def test_optional_fields_are_absent_without_error(self):
        mapping = _mapping(content=None, thread_id=None)
        raw = {"event": {"id": "1", "type": "message", "user": "U1", "channel": "C1", "ts": "1.0"}}
        event = mapping.apply(platform="teams", installation_id="T2", raw=raw)
        self.assertEqual((event.content, event.thread_id), ("", None))


class FusedChannelAdapterTests(unittest.TestCase):
    def test_normalize_event_delegates_to_the_configured_mapping(self):
        adapter = FusedChannelAdapter(_config())
        raw = {"event": {"id": "1", "type": "message", "user": "U1", "channel": "C1", "ts": "1.0", "text": "hi"}}
        event = adapter.normalize_event(raw)
        self.assertEqual(event.platform, "slack")
        self.assertEqual(event.installation_id, "T1")

    def test_send_reply_calls_the_configured_operation_deterministically(self):
        adapter = FusedChannelAdapter(_config())
        reply = ChannelReply(reply_id="r1", platform="slack", installation_id="T1",
                             conversation_id="C1", reply_to={}, content="hello")
        session = AsyncMock()
        session.call_tool.return_value = _FakeResult({"ok": True})

        @asynccontextmanager
        async def fake_resource_session(configured, framework="langgraph"):
            yield session, None

        with patch("harnest.mcp_resources.resource_session", fake_resource_session):
            import asyncio

            result = asyncio.run(adapter.send_reply(reply))
        session.call_tool.assert_awaited_once_with(
            "postMessage", {"channel": "C1", "text": "hello", "kind": "reply"},
        )
        self.assertEqual(result, {"ok": True})

    def test_config_requires_platform_installation_and_operation(self):
        for overrides in ({"platform": ""}, {"installation_id": ""}, {"reply_operation": ""}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                _config(**overrides)


class _FakeResult:
    """A minimal CallToolResult double exposing model_dump like the real SDK type."""

    def __init__(self, payload):
        self._payload = payload

    def model_dump(self, **_kwargs):
        return self._payload
