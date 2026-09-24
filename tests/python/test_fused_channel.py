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

    def test_registered_adapter_builds_from_plain_data(self):
        self.assertIn("fused", registered_channel_adapters())
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
    def test_adapter_maps_required_and_optional_event_fields(self):
        raw = {"event": {"id": "1", "type": "message", "user": "U1", "channel": "C1",
                         "ts": "1700000000.1", "text": "hi", "thread_ts": "1700000000.0"}}
        for platform, installation, mapping, expected in (
            ("slack", "T1", _mapping(), ("hi", "1700000000.0")),
            ("teams", "T2", _mapping(content=None, thread_id=None), ("", None)),
        ):
            with self.subTest(platform=platform):
                adapter = FusedChannelAdapter(_config(platform=platform, installation_id=installation, mapping=mapping))
                event = adapter.normalize_event(raw)
                self.assertEqual((event.platform, event.installation_id, event.sender_id), (platform, installation, "U1"))
                self.assertEqual(event.conversation_id, "C1")
                self.assertEqual((event.content, event.thread_id), expected)
                self.assertEqual(event.occurred_at, 1700000000.1)

    def test_missing_required_field_raises_channel_error(self):
        with self.assertRaises(ChannelError):
            _mapping().apply(platform="slack", installation_id="T1", raw={"event": {"id": "1"}})


class FusedChannelAdapterTests(unittest.TestCase):

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
