"""Verify live configuration from authored YAML through real WebSocket handshakes."""

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from harnest.bundle import compile_artifact
from harnest.runtime import _load_server
from harnest.server_config import (
    DEFAULT_SERVER_YAML,
    ServerConfigError,
    load_server_config,
    project_server_config_yaml,
)
from _session_store_fixture import write_session_store


class LiveConfigTests(unittest.TestCase):
    def test_live_is_a_strict_optional_boolean_or_startup_reference(self):
        """Inline config uses false by default and resolves explicit boolean templates."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for setting in (False, True, "${LIVE}"):
                (root / "config.yaml").write_text(yaml.safe_dump({"server": {"live": setting}}))
                contents = project_server_config_yaml(root)
                compiled = root / "compiled.yaml"
                compiled.write_text(contents)
                decoded = load_server_config(compiled, environment={"LIVE": "true"})
                self.assertEqual(decoded.live, setting is not False)
            for setting in (None, 0, 1, "true", [], {}, "$LIVE", "${LIVE}-suffix"):
                (root / "config.yaml").write_text(yaml.safe_dump({"server": {"live": setting}}))
                with self.subTest(setting=setting), self.assertRaises(ServerConfigError):
                    project_server_config_yaml(root)

    def test_legacy_documents_keep_websockets_unless_explicitly_disabled(self):
        """Existing server files retain their previous transport availability."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server.yaml"
            path.write_text(DEFAULT_SERVER_YAML.replace("live: false\n", ""))
            self.assertTrue(load_server_config(path).live)
            path.write_text(DEFAULT_SERVER_YAML)
            self.assertFalse(load_server_config(path).live)

    def test_live_environment_errors_do_not_echo_values(self):
        """Invalid deployment values identify the field without disclosing content."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server.yaml"
            path.write_text(DEFAULT_SERVER_YAML.replace("live: false", "live: ${LIVE}"))
            for environment in ({}, {"LIVE": ""}, {"LIVE": "private-value"}):
                with self.subTest(environment=environment), self.assertRaises(ServerConfigError) as caught:
                    load_server_config(path, environment=environment)
                self.assertIn("live", str(caught.exception))
                self.assertNotIn("private-value", str(caught.exception))


@unittest.skipUnless(
    importlib.util.find_spec("google.adk") and importlib.util.find_spec("langgraph"),
    "framework dependencies are not installed",
)
class LiveServerTests(unittest.TestCase):
    def _server(self, root, framework, mode, settings):
        """Compile a model-free managed graph or an uninvoked native ADK agent."""
        source = root / "source"
        source.mkdir()
        agent = '''from harnest.graph import START, Edge, Graph


def respond(value):
    """Return deterministic output without calling a model."""
    return "hello"


root_agent = Graph(name="live_fixture", nodes={"respond": respond}, edges=(Edge(START, "respond"),))
'''
        # A native ADK app exposes additional WebSockets that share the same server policy.
        if mode == "advanced":
            agent = '''from google.adk.agents import LlmAgent
from harnest.agent import Agent

root_agent = Agent.advanced(LlmAgent(name="live_fixture", model="gemini-test"))
'''
        (source / "agent.py").write_text(agent)
        (source / "instructions.md").write_text("Answer clearly.\n")
        (source / "agent-card.yaml").write_text("name: Live fixture\ndescription: Offline transport test.\nversion: 1.0.0\n")
        write_session_store(source)
        (source / "config.yaml").write_text(yaml.safe_dump({"server": settings}))
        artifact = root / "artifact"
        compile_artifact(source, artifact, framework=framework, mode=mode)
        args = SimpleNamespace(artifact=artifact, host=None, port=None,
                               request_timeout=None, max_concurrency=None, allow_remote=None)
        app, _http = _load_server(args)
        return app

    def test_default_and_explicit_false_deny_all_websocket_upgrades(self):
        """Disabled policy preserves HTTP while blocking neutral, native, and custom sockets."""
        for framework, mode in (("adk", "managed"), ("langgraph", "managed"), ("adk", "advanced")):
            for settings in ({}, {"live": False}):
                with self.subTest(framework=framework, mode=mode, settings=settings), tempfile.TemporaryDirectory() as directory:
                    app = self._server(Path(directory), framework, mode, settings)
                    calls = []

                    @app.websocket("/custom-socket")
                    async def custom_socket(websocket: WebSocket):
                        """Record entry so disabled policy cannot silently run authored handlers."""
                        calls.append("entered")
                        await websocket.accept()

                    # Native ADK validates the Host header against the configured loopback bind.
                    with TestClient(app, base_url="http://127.0.0.1") as client:
                        self.assertEqual(client.get("/healthz").status_code, 200)
                        self.assertNotIn("live", client.get("/agent").json()["endpoints"])
                        self.assertEqual(client.post("/sessions", json={}).status_code, 201)
                        for path in ("/live", "/run_live", "/custom-socket"):
                            with self.assertRaises(WebSocketDisconnect) as caught:
                                with client.websocket_connect(path):
                                    self.fail("disabled WebSocket was accepted")
                            self.assertEqual(caught.exception.code, 1008)
                    self.assertEqual(calls, [])

    def test_true_enables_live_handshake_for_each_backend(self):
        """The compiled startup setting exposes discovery and a usable live session."""
        for framework, mode in (("adk", "managed"), ("langgraph", "managed"), ("adk", "advanced")):
            with self.subTest(framework=framework, mode=mode), tempfile.TemporaryDirectory() as directory:
                with patch.dict(os.environ, {"LIVE": "true"}):
                    app = self._server(Path(directory), framework, mode, {"live": "${LIVE}"})
                with TestClient(app, base_url="http://127.0.0.1") as client:
                    self.assertEqual(client.get("/agent").json()["endpoints"]["live"], "/live")
                    with client.websocket_connect("/live") as socket:
                        socket.send_json({"type": "connect"})
                        self.assertEqual(socket.receive_json()["type"], "session.connected")
                        socket.send_json({"type": "session.close"})
