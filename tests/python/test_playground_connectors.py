"""Exercise the OAuth flow, Admin-client operations, and route gating for connectors."""

import json
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from harnest.neutral_runtime import create_neutral_app
from harnest.playground_connectors import (
    ConnectorError,
    PlaygroundConnectorsService,
    _CreateServiceRef,
    _token_env,
)
from test_neutral_runtime import FakeDriver


class _TransportUrls:
    """A minimal stand-in for the Admin client's transport URL dataclass."""

    def __init__(self, versioned="https://engine.test/mcp/pinned", streamable="https://engine.test/mcp/family"):
        self.versioned_streamable_http = versioned
        self.streamable_http = streamable
        self.sse = f"{streamable}/sse"
        self.versioned_sse = f"{versioned}/sse"


class _Server:
    """A minimal stand-in for the Admin client's MCPServer dataclass."""

    def __init__(self, name, version, active=True, urls=None):
        self.id = f"{name}-{version}"
        self.name = name
        self.version = version
        self.active = active
        self.stable = True
        self.transport_urls = urls or _TransportUrls()


class _ServerList:
    def __init__(self, items):
        self.items = items
        self.total = len(items)


class FakeAuthClient:
    """Pretend to be FusedAuthClient without any network access."""

    def __init__(self, config):
        self.config = config

    def authorize_url(self, scopes=None, state=None):
        return type("AuthorizeRequest", (), {
            "url": f"https://engine.test/oauth/authorize?state={state}",
            "state": state,
            "code_verifier": "verifier",
        })()

    def exchange_code(self, code, verifier):
        if verifier != "verifier":
            raise ConnectorError("bad verifier")
        return type("TokenResponse", (), {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
            "scope": "app.read",
        })()


class FakeAdminClient:
    """Pretend to be FusedAdminClient, returning scripted server state."""

    servers = []
    token_value = "tok-1"
    last_deployed = None

    def __init__(self, config):
        self.config = config

    def list_servers(self, limit=10, offset=0):
        return _ServerList(self.servers[offset : offset + limit])

    def list_services(self):
        return [
            {"id": "svc-a", "version_id": "svc-a-v1", "slug": "verify-service-a", "name": "Verify Service A", "description": "", "version": "1.0.27"},
            {"id": "svc-b", "version_id": "svc-b-v1", "slug": "verify-service-b", "name": "Verify Service B", "description": "", "version": "1.0.27"},
        ]

    def list_service_operations(self, service_id, version):
        return [
            {"id": "op-1", "name": "addPet", "method": "POST", "path": "/pet"},
            {"id": "op-2", "name": "deletePet", "method": "DELETE", "path": "/pet/{petId}"},
        ]

    def deploy_server(self, config):
        FakeAdminClient.last_deployed = config
        return _Server(config.get("name", "studio-mcp"), "1.0.0")

    def generate_token(self, reference, payload):
        return type("AppToken", (), {"token": self.token_value, "name": payload.name})()


def _session(access_token="access-token", expires_in=3600):
    """Build a live in-memory session without going through the OAuth flow."""

    return type("_Session", (), {
        "access_token": access_token,
        "refresh_token": None,
        "expires_at": time.time() + expires_in,
    })()


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


def _configured_service():
    return PlaygroundConnectorsService(engine_url="https://engine.test", registration_key="frk_test")


class ServiceTests(unittest.TestCase):
    def test_available_requires_engine_and_registration_key(self):
        self.assertFalse(PlaygroundConnectorsService().available())
        self.assertFalse(PlaygroundConnectorsService(engine_url="https://engine.test").available())
        self.assertTrue(_configured_service().available())

    def test_connected_reflects_live_session(self):
        service = _configured_service()
        self.assertFalse(service.connected("sess1"))
        service._sessions["sess1"] = _session()
        self.assertTrue(service.connected("sess1"))
        service._sessions["sess1"] = _session(expires_in=-10)
        self.assertFalse(service.connected("sess1"))

    def test_authorize_then_complete_binds_session(self):
        service = _configured_service()
        with patch("harnest.playground_connectors.FusedAuthClient", FakeAuthClient), \
             patch.object(service, "_register_client", return_value=("foc_dynamic", "")):
            url = service.authorize_url("http://localhost:8000/_harnest/connectors/oauth/callback")
            self.assertIn("/oauth/authorize?", url)
            state = next(iter(service._flows))
            service.complete_authorization("code", state, "http://localhost:8000/_harnest/connectors/oauth/callback", "sess1")
        self.assertTrue(service.connected("sess1"))
        self.assertEqual(service._sessions["sess1"].access_token, "access-token")

    def test_replayed_state_fails_closed(self):
        service = _configured_service()
        with patch("harnest.playground_connectors.FusedAuthClient", FakeAuthClient), \
             patch.object(service, "_register_client", return_value=("foc_dynamic", "")):
            service.authorize_url("http://localhost:8000/_harnest/connectors/oauth/callback")
            state = next(iter(service._flows))
            service.complete_authorization("code", state, "http://localhost:8000/_harnest/connectors/oauth/callback", "sess1")
            with self.assertRaises(ConnectorError):
                service.complete_authorization("again", state, "http://localhost:8000/_harnest/connectors/oauth/callback", "sess2")

    def test_servers_projects_wire_shape(self):
        service = _configured_service()
        service._sessions["sess1"] = _session()
        FakeAdminClient.servers = [_Server("support-agent", "1.0.0")]
        with patch("harnest.playground_connectors.FusedAdminClient", FakeAdminClient):
            result = service.servers("sess1")
        self.assertEqual(result["total"], 1)
        item = result["items"][0]
        self.assertEqual(item["name"], "support-agent")
        self.assertEqual(item["transport_urls"]["versioned_streamable_http"], "https://engine.test/mcp/pinned")

    def test_add_existing_resolves_pinned_url_and_mints_token(self):
        service = _configured_service()
        service._sessions["sess1"] = _session()
        FakeAdminClient.servers = [_Server("support-agent", "1.0.0")]
        with patch("harnest.playground_connectors.FusedAdminClient", FakeAdminClient):
            result = service.add_existing("sess1", name="support-agent", version="1.0.0")
        self.assertEqual(result.url, "https://engine.test/mcp/pinned")
        self.assertEqual(result.token_env, "HARNEST_MCP_SUPPORT_AGENT_TOKEN")
        self.assertEqual(result.token, "tok-1")

    def test_add_existing_rejects_inactive_version(self):
        service = _configured_service()
        service._sessions["sess1"] = _session()
        FakeAdminClient.servers = [_Server("support-agent", "1.0.0", active=False)]
        with patch("harnest.playground_connectors.FusedAdminClient", FakeAdminClient):
            with self.assertRaises(ConnectorError):
                service.add_existing("sess1", name="support-agent", version="1.0.0")

    def test_create_builds_config_from_structured_fields(self):
        service = _configured_service()
        service._sessions["sess1"] = _session()
        with patch("harnest.playground_connectors.FusedAdminClient", FakeAdminClient):
            result = service.create(
                "sess1",
                name="studio-mcp",
                description="Studio helper",
                services=[_CreateServiceRef(slug="verify-service-a", version="1.0.27")],
            )
        self.assertEqual(FakeAdminClient.last_deployed, {
            "apiVersion": "fused/v1",
            "kind": "mcp",
            "name": "studio-mcp",
            "version": "1.0.0",
            "description": "Studio helper",
            "bucket": "default",
            "services": {"verify-service-a": {"version": "1.0.27", "select_all": True}},
        })
        self.assertEqual(result.name, "studio-mcp")
        self.assertEqual(result.url, "https://engine.test/mcp/pinned")
        self.assertEqual(result.token, "tok-1")

    def test_create_defaults_description_to_name(self):
        # The Engine rejects an empty MCP description, so the Studio falls
        # back to the server name before deploying.
        service = _configured_service()
        service._sessions["sess1"] = _session()
        with patch("harnest.playground_connectors.FusedAdminClient", FakeAdminClient):
            service.create(
                "sess1",
                name="studio-mcp",
                description="",
                services=[_CreateServiceRef(slug="verify-service-a", version="1.0.27")],
            )
        self.assertEqual(FakeAdminClient.last_deployed["description"], "studio-mcp")

    def test_create_narrows_service_to_operations(self):
        service = _configured_service()
        service._sessions["sess1"] = _session()
        with patch("harnest.playground_connectors.FusedAdminClient", FakeAdminClient):
            service.create(
                "sess1",
                name="studio-mcp",
                description="",
                services=[_CreateServiceRef(slug="verify-service-a", version="1.0.27", select_all=False, operations=["addPet"])],
            )
        self.assertEqual(
            FakeAdminClient.last_deployed["services"]["verify-service-a"],
            {"version": "1.0.27", "operations": ["addPet"]},
        )

    def test_operations_lists_service_operations(self):
        service = _configured_service()
        service._sessions["sess1"] = _session()
        with patch("harnest.playground_connectors.FusedAdminClient", FakeAdminClient):
            result = service.operations("sess1", "svc-a", "svc-a-v1")
        self.assertEqual(result["items"][0]["name"], "addPet")

    def test_require_token_rejects_missing_and_expired(self):
        service = _configured_service()
        with self.assertRaises(ConnectorError):
            service.servers("missing")
        service._sessions["sess1"] = _session(expires_in=-10)
        with self.assertRaises(ConnectorError):
            service.servers("sess1")

    def test_services_lists_workspace_services(self):
        service = _configured_service()
        service._sessions["sess1"] = _session()
        with patch("harnest.playground_connectors.FusedAdminClient", FakeAdminClient):
            result = service.services("sess1")
        self.assertEqual(result["items"][0]["slug"], "verify-service-a")

    def test_token_env_derives_a_collision_resistant_name(self):
        self.assertEqual(_token_env("support-agent"), "HARNEST_MCP_SUPPORT_AGENT_TOKEN")

    def test_register_client_sends_bearer_key(self):
        service = _configured_service()
        captured = {}

        def fake_urlopen(request):
            captured["url"] = request.full_url
            captured["headers"] = request.headers
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _JSONResponse({"client_id": "foc_dynamic", "client_id_expires_at": ""})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            client_id, _ = service._register_client("http://localhost:4321/cb", "Harnest Studio")
        self.assertEqual(client_id, "foc_dynamic")
        self.assertEqual(captured["url"], "https://engine.test/oauth/register")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer frk_test")
        self.assertEqual(captured["body"]["redirect_uri"], "http://localhost:4321/cb")


class RouteTests(unittest.TestCase):
    def client(self, service):
        app = create_neutral_app(FakeDriver(), playground_connectors_service=service)
        client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234))
        self.addCleanup(client.close)
        return client

    def test_status_reports_unconfigured(self):
        client = self.client(PlaygroundConnectorsService())
        response = client.get("/_harnest/connectors")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"available": False, "connected": False})

    def test_servers_409_without_a_connection(self):
        client = self.client(_configured_service())
        self.assertEqual(client.get("/_harnest/connectors/servers").status_code, 409)

    def test_oauth_start_and_callback_end_to_end(self):
        service = _configured_service()
        client = self.client(service)
        with patch("harnest.playground_connectors.FusedAuthClient", FakeAuthClient), \
             patch.object(service, "_register_client", return_value=("foc_dynamic", "")):
            start = client.post("/_harnest/connectors/oauth/start")
            self.assertEqual(start.status_code, 200)
            self.assertIn("/oauth/authorize?", start.json()["url"])
            session_id = start.cookies["_harnest_fused_session"]
            state = next(iter(service._flows))
            callback = client.get(
                "/_harnest/connectors/oauth/callback",
                params={"code": "code", "state": state},
                cookies={"_harnest_fused_session": session_id},
            )
            self.assertEqual(callback.status_code, 200)
        self.assertTrue(service.connected(session_id))
        status = client.get("/_harnest/connectors", cookies={"_harnest_fused_session": session_id})
        self.assertTrue(status.json()["connected"])

    def test_oauth_callback_rejects_forged_state(self):
        service = _configured_service()
        client = self.client(service)
        response = client.get(
            "/_harnest/connectors/oauth/callback",
            params={"code": "code", "state": "forged"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("state", response.text.lower())

    def test_servers_route_returns_listed_servers_when_connected(self):
        service = _configured_service()
        service._sessions["sess1"] = _session()
        FakeAdminClient.servers = [_Server("support-agent", "1.0.0")]
        client = self.client(service)
        with patch("harnest.playground_connectors.FusedAdminClient", FakeAdminClient):
            response = client.get(
                "/_harnest/connectors/servers",
                cookies={"_harnest_fused_session": "sess1"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["name"], "support-agent")

    def test_add_route_rejects_non_local_callers(self):
        service = _configured_service()
        service._sessions["sess1"] = _session()
        app = create_neutral_app(FakeDriver(), playground_connectors_service=service)
        client = TestClient(app, base_url="http://127.0.0.1", client=("203.0.113.5", 1234))
        self.addCleanup(client.close)
        response = client.post(
            "/_harnest/connectors/add", json={"name": "support-agent", "version": "1.0.0"}
        )
        self.assertEqual(response.status_code, 403)

    def test_create_route_rejects_missing_services(self):
        # An empty service list is rejected by request validation before it
        # reaches the deployment call, so the framework answers with 422.
        service = _configured_service()
        service._sessions["sess1"] = _session()
        client = self.client(service)
        response = client.post(
            "/_harnest/connectors/create",
            json={"name": "x", "services": []},
            cookies={"_harnest_fused_session": "sess1"},
        )
        self.assertEqual(response.status_code, 422)

    def test_create_route_end_to_end(self):
        service = _configured_service()
        service._sessions["sess1"] = _session()
        client = self.client(service)
        with patch("harnest.playground_connectors.FusedAdminClient", FakeAdminClient):
            response = client.post(
                "/_harnest/connectors/create",
                json={
                    "name": "studio-mcp",
                    "description": "Studio helper",
                    "services": [{"slug": "verify-service-a", "version": "1.0.27"}],
                },
                cookies={"_harnest_fused_session": "sess1"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "name": "studio-mcp",
            "url": "https://engine.test/mcp/pinned",
            "tokenEnv": "HARNEST_MCP_STUDIO_MCP_TOKEN",
            "token": "tok-1",
        })


if __name__ == "__main__":
    unittest.main()
