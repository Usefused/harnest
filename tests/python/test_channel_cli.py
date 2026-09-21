"""Released developer CLI for inspecting and fixture-testing channel bindings."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from harnest.channels import ChannelAdapter, ChannelEvent, _ADAPTER_FACTORIES, register_channel_adapter
from harnest.cli import main


class _EchoAdapter(ChannelAdapter):
    platform = "stub"

    def normalize_event(self, raw):
        return ChannelEvent(
            platform="stub", installation_id=raw["installation_id"],
            provider_event_id=raw["id"], kind="message", sender_id=raw["sender"],
            conversation_id=raw["conversation"], message_id=raw["id"],
            occurred_at=0.0, content=raw.get("text", ""),
        )

    async def send_reply(self, reply):
        return {"ok": True}


def fixture(root):
    (root / "channels").mkdir()
    (root / "channels" / "slack.py").write_text(
        "from harnest.channels import ChannelBinding\n"
        "def binding():\n"
        "    return ChannelBinding(platform='slack', extension='cli-test-stub')\n"
    )


class ChannelsCLITests(unittest.TestCase):
    def setUp(self):
        register_channel_adapter("cli-test-stub", lambda config: _EchoAdapter())
        self.addCleanup(_ADAPTER_FACTORIES.pop, "cli-test-stub", None)

    def test_inspect_reports_binding_scope_and_extension_registration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture(root)
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["channels", "inspect", "--project", temporary, "--json"])
            self.assertEqual(code, 0)
            result = json.loads(output.getvalue())
            self.assertIn("cli-test-stub", result["available_extensions"])
            self.assertEqual(result["bindings"], [{
                "platform": "slack", "extension": "cli-test-stub",
                "extension_registered": True,
                "allowed_installations": [], "allowed_conversations": [],
            }])

    def test_test_normalizes_a_fixture_offline_without_sending(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture(root)
            fixture_path = root / "message.json"
            fixture_path.write_text(json.dumps(
                {"id": "e1", "installation_id": "T1", "sender": "alice",
                 "conversation": "c1", "text": "hello"}
            ))
            output = io.StringIO()
            with redirect_stdout(output):
                code = main([
                    "channels", "test", "--project", temporary,
                    "--fixture", str(fixture_path), "--json",
                ])
            self.assertEqual(code, 0)
            result = json.loads(output.getvalue())
            self.assertEqual(len(result["results"]), 1)
            self.assertEqual(result["results"][0]["event"]["sender_id"], "alice")

    def test_test_without_fixture_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture(root)
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["channels", "test", "--project", temporary])
            self.assertEqual(code, 2)
