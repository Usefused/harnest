"""Exercise build identity, read-only transport, and source-preview isolation."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from harnest.neutral_runtime import create_neutral_app
from harnest.playground_studio import PlaygroundStudioService
from test_neutral_runtime import FakeDriver, HeaderAuthenticator


class PlaygroundStudioTests(unittest.TestCase):
    """Keep the Studio surface scoped to its served build and authenticated caller."""

    def setUp(self):
        """Build an inert source fixture containing ownership and explicit workflow edges."""

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "agent.py").write_text("raise RuntimeError('must not execute')\nroot_agent = Agent(name='support', model='openai/test')\n", encoding="utf-8")
        (self.root / "tools").mkdir()
        (self.root / "lib").mkdir()
        (self.root / "lib/normalize.py").write_text("def normalize(value: str) -> str: return value\n", encoding="utf-8")
        (self.root / "tools/lookup.py").write_text(
            "from harnest.lib.normalize import normalize\n@tool\ndef lookup(query: str): return normalize(query)\n",
            encoding="utf-8",
        )
        (self.root / "instructions.md").write_text("Answer clearly. <script>never execute</script>", encoding="utf-8")
        (self.root / ".env").write_text("PRIVATE=never-read", encoding="utf-8")
        self.service = PlaygroundStudioService(self.root, mode="managed")

    def client(self, *, host="127.0.0.1", base="http://127.0.0.1", **options):
        """Construct the real neutral app with selectable network and authentication policy."""

        app = create_neutral_app(FakeDriver(), playground_studio_service=self.service, **options)
        client = TestClient(app, base_url=base, client=(host, 1234))
        self.addCleanup(client.close)
        return client

    def test_architecture_is_static_scoped_and_read_only(self):
        """The projection omits raw contents and private paths and exposes typed edges."""

        client = self.client()
        response = client.get("/_harnest/studio")
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertTrue(value["read_only"])
        self.assertTrue(value["source_available"])
        self.assertNotIn(str(self.root), response.text)
        self.assertNotIn("never-read", response.text)
        self.assertNotIn("never execute", response.text)
        self.assertEqual(value["connections"][0]["kind"], "capability")
        self.assertIn("instructions", [block["kind"] for block in value["blocks"]])
        files = {item["path"]: item["lib_imports"] for item in value["files"]}
        self.assertEqual(files["tools/lookup.py"], ["lib/normalize.py"])
        self.assertEqual(files["agent.py"], [])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(client.post("/_harnest/studio", json={}).status_code, 405)
        self.assertEqual(client.put("/_harnest/studio/source?path=agent.py", json={}).status_code, 405)
        self.assertNotIn("/_harnest/studio", client.get("/openapi.json").json()["paths"])

    def test_source_reads_only_indexed_unchanged_files(self):
        """Readers cannot select another workspace or silently inspect a modified artifact."""

        client = self.client()
        response = client.get("/_harnest/studio/source", params={"path": "instructions.md"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["text"], "Answer clearly. <script>never execute</script>")
        self.assertEqual(response.headers["cache-control"], "no-store")
        for path in ("../agent.py", str(self.root / "agent.py"), ".env", "missing.py"):
            with self.subTest(path=path):
                self.assertEqual(client.get("/_harnest/studio/source", params={"path": path}).status_code, 404)
        (self.root / "agent.py").write_text("changed = True", encoding="utf-8")
        self.assertEqual(client.get("/_harnest/studio/source", params={"path": "agent.py"}).status_code, 409)

    def test_source_rejects_links_even_after_indexing(self):
        """Replacing an indexed file with a link cannot open another file."""

        client = self.client()
        client.get("/_harnest/studio")
        (self.root / "agent.py").unlink()
        (self.root / "agent.py").symlink_to(self.root / "instructions.md")
        self.assertEqual(client.get("/_harnest/studio/source", params={"path": "agent.py"}).status_code, 404)

    def test_source_rejects_oversized_previews(self):
        """Local preview reads stay bounded even for otherwise valid indexed files."""

        (self.root / "large.md").write_text("x" * (1024 * 1024 + 1), encoding="utf-8")
        response = self.client().get("/_harnest/studio/source", params={"path": "large.md"})
        self.assertEqual(response.status_code, 413)

    def test_remote_and_cross_origin_source_requests_are_denied(self):
        """Loopback transport alone does not authorize hostile browser origins or hosts."""

        cases = [
            ({"host": "192.0.2.1"}, {}),
            ({"base": "http://evil.example"}, {}),
            ({}, {"origin": "http://evil.example"}),
            ({}, {"sec-fetch-site": "cross-site"}),
        ]
        for options, headers in cases:
            with self.subTest(options=options, headers=headers):
                client = self.client(**options)
                metadata = client.get("/_harnest/studio", headers=headers)
                self.assertFalse(metadata.json()["source_available"])
                self.assertEqual(client.get("/_harnest/studio/source?path=agent.py", headers=headers).status_code, 403)

    def test_authentication_protects_projection_and_source_but_not_assets(self):
        """Architecture is private runtime data, while browser assets remain public."""

        client = self.client(authenticator=HeaderAuthenticator())
        for path in ("/_harnest/studio", "/_harnest/authoring", "/_harnest/studio/source?path=agent.py"):
            self.assertEqual(client.get(path).status_code, 401)
            self.assertEqual(client.get(path, headers={"x-test-user": "alice"}).status_code, 200)
        for path in ("/_harnest/studio.js", "/_harnest/studio.css", "/_harnest/builder.js", "/_harnest/builder.css", "/_harnest/selects.js", "/_harnest/selects.css"):
            self.assertEqual(client.get(path).status_code, 200)

    def test_disabled_playground_and_missing_assets_expose_no_studio(self):
        """Production UI policy removes data and browser routes together."""

        clients = [self.client(playground_enabled=False)]
        with patch("harnest.playground.playground_available", return_value=False):
            clients.append(self.client())
        for client in clients:
            for path in ("/_harnest/studio", "/_harnest/authoring", "/_harnest/studio.js", "/_harnest/builder.js", "/_harnest/studio.css", "/_harnest/studio/source?path=agent.py"):
                self.assertEqual(client.get(path).status_code, 404)

    def test_advanced_mode_does_not_invent_folder_owned_connections(self):
        """An advanced framework owns its graph regardless of adjacent resource folders."""

        self.service = PlaygroundStudioService(self.root, mode="advanced")
        self.assertEqual(self.client().get("/_harnest/studio").json()["connections"], [])

    def test_unavailable_source_has_actionable_safe_error(self):
        """Missing build files fail without leaking host paths or serving stale results."""

        self.service = PlaygroundStudioService(self.root / "absent")
        response = self.client().get("/_harnest/studio")
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(str(self.root), response.text)
        with TestClient(create_neutral_app(FakeDriver())) as client:
            self.assertEqual(client.get("/_harnest/studio").status_code, 404)

    def test_compiled_frameworks_expose_the_build_not_authoring_folder(self):
        """Exercise the compiler and both neutral and native ADK serving paths."""

        from harnest.bundle import compile_artifact
        from harnest.runtime import create_fastapi_app

        for framework, mode in (("adk", "managed"), ("langgraph", "managed"), ("adk", "advanced")):
            with self.subTest(framework=framework, mode=mode):
                authored = self.root / f"{framework}-{mode}"
                _write_agent(authored, framework, mode)
                artifact = self.root / f"build-{framework}-{mode}"
                compile_artifact(authored, artifact, framework=framework, mode=mode)
                (authored / "instructions.md").write_text("Uncompiled changes", encoding="utf-8")
                with TestClient(create_fastapi_app(artifact), base_url="http://127.0.0.1", client=("127.0.0.1", 1234)) as client:
                    response = client.get("/_harnest/studio")
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertTrue(any(block["name"] == "root_agent" for block in response.json()["blocks"]))
                    source = client.get("/_harnest/studio/source", params={"path": "instructions.md"})
                    self.assertEqual(source.json()["text"], "Compiled instructions")
                    self.assertIn('data-workspace="studio"', client.get("/").text)
                    self.assertFalse(client.get("/_harnest/authoring").json()["available"])
                self.assert_authoring_boundary(authored, artifact)

    def assert_authoring_boundary(self, authored, artifact):
        """Both runtime adapters capture explicit source ownership and leave artifacts immutable."""

        from harnest.playground_authoring import authoring_workspace
        from harnest.runtime import create_fastapi_app

        with authoring_workspace(authored):
            app = create_fastapi_app(artifact)
        with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234)) as client:
            self.assertTrue(client.get("/_harnest/authoring").json()["available"])
            document = client.get("/_harnest/authoring/document?path=instructions.md").json()
            self.assertEqual(document["text"], "Uncompiled changes")
            response = client.put("/_harnest/authoring/document", json={**document, "text": "Edited locally"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual((authored / "instructions.md").read_text(), "Edited locally")
            self.assertEqual(client.get("/_harnest/studio/source?path=instructions.md").json()["text"], "Compiled instructions")

    def test_browser_studio_behavior(self):
        """Run behavior tests for production browser rendering and view switching."""

        script = Path(__file__).resolve().parents[1] / "javascript/playground_studio.cjs"
        subprocess.run(["node", "--test", str(script)], check=True, capture_output=True, text=True)


def _write_agent(root: Path, framework: str, mode: str) -> None:
    """Create offline runnable agents for both serving boundaries without provider calls."""

    root.mkdir()
    source = "from harnest.graph import START, Edge, Event, Graph\ndef respond(value): return Event(message='hello')\nroot_agent = Graph(name='root', nodes={'respond': respond}, edges=(Edge(START, 'respond'),))\n"
    if mode == "advanced":
        source = "from google.adk.agents import LlmAgent\nfrom harnest.agent import Agent\nroot_agent = Agent.advanced(LlmAgent(name='root', model='test-model'))\n"
    (root / "lifecycle").mkdir()
    (root / "lifecycle/storage.py").write_text(
        "from harnest import lifecycle\nfrom harnest.session import InMemorySessionStore\nfrom harnest.checkpoint import MemoryStore\n"
        "@lifecycle.storage.sessions\ndef sessions(): return InMemorySessionStore()\n"
        "@lifecycle.storage.checkpoints\ndef checkpoints(): return MemoryStore()\n", encoding="utf-8",
    )
    (root / "agent.py").write_text(source, encoding="utf-8")
    (root / "instructions.md").write_text("Compiled instructions", encoding="utf-8")
    (root / "agent-card.yaml").write_text(json.dumps({"name": "Root", "description": "Studio test"}), encoding="utf-8")
