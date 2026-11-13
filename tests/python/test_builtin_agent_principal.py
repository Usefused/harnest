"""Built-in routes get Agent Runtime Principals from one lifecycle hook.

Custom routes pass ``agent_principal=`` themselves. ``/responses``, live,
AG-UI and A2A cannot, so ``@lifecycle.http.agent_principal`` grants their
permissions, and ``server.agentPrincipal: required`` refuses them when no
hook exists.
"""

from dataclasses import replace
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from _session_store_fixture import write_session_store
from harnest.agent import AgentRuntimePrincipal
from harnest.extension_loader import ExtensionDiscoveryError, discover_extensions
from harnest.http_agent_principal import builtin_principal_policy
from harnest.lifecycle import LifecycleListener
from harnest.neutral_runtime import create_neutral_app
from harnest.runtime_auth import ConnectionContext
from test_a2a import _driver as _a2a_driver, _send_payload
from test_neutral_runtime import FakeDriver, HeaderAuthenticator


def _grant_listener(callback, *, line=1):
    """Build the listener discovery would produce for one authored hook."""

    return LifecycleListener(
        phase="http_agent_principal",
        callback=callback,
        order=0,
        relative_path="lifecycle/http.py",
        line=line,
        function_name="grant",
    )


def _grant_by_user(connection, principal):
    """Map the authenticated user onto permissions, as an application would."""

    # Hooks see the same read-only ConnectionContext as `authenticate`.
    assert isinstance(connection, ConnectionContext), type(connection)
    assert connection.headers["x-test-user"] == principal.user_id
    permissions = {"refunds.write"} if principal.user_id == "admin" else set()
    return AgentRuntimePrincipal.create(permissions={"tickets.read", *permissions})


def _projecting(driver):
    """Principals are only accepted where tool projection is complete."""

    driver.info = replace(driver.info, agent_principal_projection_complete=True)
    return driver


def _app(driver, *, grant=None, required=False, **options):
    listeners = () if grant is None else (_grant_listener(grant),)
    return create_neutral_app(
        driver,
        authenticator=HeaderAuthenticator(),
        lifecycle_extensions=listeners,
        agent_principal_required=required,
        playground_enabled=False,
        **options,
    )


_USER = {"x-test-user": "alice"}


class ResponsesRouteTests(unittest.TestCase):
    def test_optional_without_hook_keeps_compatibility_mode(self):
        driver = FakeDriver()
        with TestClient(_app(driver)) as client:
            response = client.post("/responses", json={"input": "hi"}, headers=_USER)

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(driver.invocations[0].agent_principal)

    def test_required_without_hook_refuses_before_session_creation(self):
        driver = FakeDriver()
        with TestClient(_app(driver, required=True)) as client:
            response = client.post("/responses", json={"input": "hi"}, headers=_USER)

        self.assertEqual(response.status_code, 403)
        self.assertIn("@lifecycle.http.agent_principal", response.json()["detail"])
        self.assertEqual(driver.sessions, {})
        self.assertEqual(driver.invocations, [])

    def test_hook_grants_permissions_per_authenticated_user(self):
        for required in (False, True):
            for user, expected in (
                ("alice", {"tickets.read"}),
                ("admin", {"tickets.read", "refunds.write"}),
            ):
                with self.subTest(required=required, user=user):
                    driver = _projecting(FakeDriver())
                    app = _app(driver, grant=_grant_by_user, required=required)
                    with TestClient(app) as client:
                        response = client.post(
                            "/responses",
                            json={"input": "hi"},
                            headers={"x-test-user": user},
                        )

                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(
                        driver.invocations[0].agent_principal.permissions,
                        frozenset(expected),
                    )

    def test_streaming_responses_carry_the_granted_principal(self):
        driver = _projecting(FakeDriver())
        with TestClient(_app(driver, grant=_grant_by_user)) as client:
            response = client.post(
                "/responses", json={"input": "hi", "stream": True}, headers=_USER
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            driver.invocations[0].agent_principal.permissions,
            frozenset({"tickets.read"}),
        )

    def test_async_hook_is_awaited(self):
        async def grant(connection, principal):
            del connection, principal
            return AgentRuntimePrincipal.create(permissions={"tickets.read"})

        driver = _projecting(FakeDriver())
        with TestClient(_app(driver, grant=grant)) as client:
            response = client.post("/responses", json={"input": "hi"}, headers=_USER)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            driver.invocations[0].agent_principal.permissions,
            frozenset({"tickets.read"}),
        )

    def test_hook_returning_none_denies_instead_of_widening_access(self):
        driver = FakeDriver()
        with TestClient(_app(driver, grant=lambda connection, principal: None)) as client:
            response = client.post("/responses", json={"input": "hi"}, headers=_USER)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(driver.sessions, {})

    def test_hook_returning_the_wrong_type_fails_closed(self):
        driver = FakeDriver()
        app = _app(driver, grant=lambda connection, principal: {"tickets.read"})
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/responses", json={"input": "hi"}, headers=_USER)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(driver.invocations, [])

    def test_only_one_hook_is_allowed(self):
        listeners = (_grant_listener(_grant_by_user, line=1), _grant_listener(_grant_by_user, line=9))
        with self.assertRaisesRegex(ValueError, "only one"):
            builtin_principal_policy(listeners, required=False)
        with self.assertRaisesRegex(ValueError, "only one"):
            create_neutral_app(FakeDriver(), lifecycle_extensions=listeners)


class StreamingTransportTests(unittest.TestCase):
    def _agui(self, client):
        return client.post(
            "/agui",
            headers=_USER,
            json={
                "threadId": "thread-1",
                "runId": "run-1",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    def test_agui_refuses_or_grants_like_responses(self):
        refused = FakeDriver()
        with TestClient(_app(refused, required=True)) as client:
            self.assertEqual(self._agui(client).status_code, 403)
        self.assertEqual(refused.sessions, {})

        granted = _projecting(FakeDriver())
        with TestClient(_app(granted, grant=_grant_by_user, required=True)) as client:
            self.assertEqual(self._agui(client).status_code, 200)
        self.assertEqual(
            granted.invocations[-1].agent_principal.permissions,
            frozenset({"tickets.read"}),
        )

    def test_live_refuses_handshake_without_a_grant(self):
        driver = FakeDriver()
        with TestClient(_app(driver, required=True, live_enabled=True)) as client:
            # Closing before accept is an HTTP 403 handshake denial.
            with self.assertRaises(WebSocketDenialResponse) as raised:
                with client.websocket_connect("/live", headers=_USER):
                    pass

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(driver.sessions, {})

    def test_live_frames_carry_the_granted_principal(self):
        driver = _projecting(FakeDriver())
        app = _app(driver, grant=_grant_by_user, required=True, live_enabled=True)
        with TestClient(app) as client:
            client.post("/sessions", json={"id": "live"}, headers=_USER)
            with client.websocket_connect("/live", headers=_USER) as websocket:
                websocket.send_json({"type": "connect", "sessionId": "live"})
                websocket.receive_json()
                websocket.send_json({"type": "response.create", "input": "hi"})
                while websocket.receive_json()["type"] not in {
                    "response.completed",
                    "error",
                }:
                    pass

        self.assertEqual(
            driver.invocations[-1].agent_principal.permissions,
            frozenset({"tickets.read"}),
        )


class A2ARouteTests(unittest.TestCase):
    def _send(self, client):
        return client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": "fixture",
                "method": "SendMessage",
                "params": _send_payload(),
            },
            headers={"A2A-Version": "1.0", **_USER},
        )

    def test_a2a_refuses_when_nothing_can_grant(self):
        driver = _a2a_driver(binding="JSONRPC")
        with TestClient(_app(driver, required=True)) as client:
            response = self._send(client)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(driver.invocations, [])

    def test_a2a_tasks_carry_the_granted_principal(self):
        driver = _projecting(_a2a_driver(binding="JSONRPC"))
        with TestClient(_app(driver, grant=_grant_by_user, required=True)) as client:
            response = self._send(client)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("result", response.json())
        self.assertEqual(
            driver.invocations[-1].agent_principal.permissions,
            frozenset({"tickets.read"}),
        )

    def test_a2a_hook_denial_never_invokes_the_agent(self):
        driver = _a2a_driver(binding="JSONRPC")
        app = _app(driver, grant=lambda connection, principal: None, required=True)
        with TestClient(app) as client:
            response = self._send(client)

        self.assertIn("error", response.json())
        self.assertEqual(driver.invocations, [])


class DiscoveryTests(unittest.TestCase):
    def test_hook_is_discovered_from_lifecycle_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "agent"
            write_session_store(root)
            hook = root / "lifecycle" / "http.py"
            hook.write_text(
                "from harnest import lifecycle\n"
                "from harnest.agent import AgentRuntimePrincipal\n"
                "@lifecycle.http.agent_principal\n"
                "def grant(connection, principal):\n"
                "    return AgentRuntimePrincipal.create(permissions=())\n",
                encoding="utf-8",
            )
            discovered = discover_extensions(root / "lifecycle", framework="adk")
            phases = [item.phase for item in discovered.listeners]
            self.assertIn("http_agent_principal", phases)

            hook.write_text(
                "from harnest import lifecycle\n"
                "@lifecycle.http.agent_principal\n"
                "def grant(principal): return None\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ExtensionDiscoveryError, "exactly two"):
                discover_extensions(root / "lifecycle", framework="adk")


if __name__ == "__main__":
    unittest.main()
