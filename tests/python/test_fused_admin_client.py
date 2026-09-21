"""Direct unit tests for the vendored Fused Admin management client."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from harnest._fused_admin_client import (
    FusedAdminClient,
    FusedAdminConfig,
    FusedAdminError,
    WebhookServiceSelection,
)


class _JSONResponse:
    """A minimal urllib response double that yields one JSON payload."""

    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _client() -> FusedAdminClient:
    return FusedAdminClient(FusedAdminConfig(engine_url="https://engine.test", access_token="tok"))


class WebhookServiceSelectionTests(unittest.TestCase):
    def test_requires_exactly_one_of_events_or_select_all(self):
        WebhookServiceSelection(events=["message.channels"])
        WebhookServiceSelection(select_all=True)
        for kwargs in ({}, {"events": ["a"], "select_all": True}):
            with self.assertRaises(ValueError):
                WebhookServiceSelection(**kwargs)

    def test_projects_only_the_selected_shape(self):
        self.assertEqual(
            WebhookServiceSelection(events=["a", "b"]).as_dict(),
            {"webhooks": ["a", "b"]},
        )
        self.assertEqual(
            WebhookServiceSelection(select_all=True, secret="${bucket.default.secret.slack}").as_dict(),
            {"webhooks_select_all": True, "secret": "${bucket.default.secret.slack}"},
        )


class ApplyWebhookConfigTests(unittest.TestCase):
    def test_sends_the_declarative_webhook_config_and_returns_applied_services(self):
        captured = {}

        def fake_urlopen(request):
            captured["url"] = request.full_url
            captured["headers"] = request.headers
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _JSONResponse({"status": "applied"})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = _client().apply_webhook_config(
                "slack-channel", {"slack": WebhookServiceSelection(events=["message.channels", "app_mention"])},
            )
        self.assertEqual(captured["url"], "https://engine.test/webhook-config/apply")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer tok")
        self.assertEqual(captured["body"], {
            "apiVersion": "fused/v1", "kind": "webhook", "name": "slack-channel",
            "services": {"slack": {"webhooks": ["message.channels", "app_mention"]}},
        })
        self.assertEqual((result.name, result.services), ("slack-channel", ["slack"]))
        self.assertEqual(result.raw, {"status": "applied"})

    def test_owner_team_is_included_only_when_given(self):
        captured = {}

        def fake_urlopen(request):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _JSONResponse({"status": "applied"})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            _client().apply_webhook_config(
                "hook", {"svc": WebhookServiceSelection(select_all=True)}, owner_team="platform",
            )
        self.assertEqual(captured["body"]["owner_team"], "platform")

    def test_requires_a_name_and_at_least_one_service(self):
        client = _client()
        with self.assertRaises(ValueError):
            client.apply_webhook_config("", {"svc": WebhookServiceSelection(select_all=True)})
        with self.assertRaises(ValueError):
            client.apply_webhook_config("hook", {})

    def test_engine_error_envelope_surfaces_as_fused_admin_error(self):
        import urllib.error

        def fake_urlopen(request):
            raise urllib.error.HTTPError(
                request.full_url, 400, "bad", None,
                fp=_ErrorBody({"message": "workspace service not connected"}),
            )

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(FusedAdminError):
                _client().apply_webhook_config("hook", {"svc": WebhookServiceSelection(select_all=True)})


class _ErrorBody:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def close(self):
        pass
