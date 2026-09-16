"""Local builder persistence, ownership, and native authoring format regression tests."""

import ast
import json
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from harnest.playground_authoring import authoring_workspace
from harnest.playground_builder import install_builder_routes
from harnest.playground_studio import PlaygroundStudioService


class PlaygroundBuilderTests(unittest.TestCase):
    """Exercise real routes against separate immutable and mutable source trees."""

    def setUp(self):
        """Create an inert graph and never import its authored module while editing."""

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root, self.compiled = base / "workspace", base / "compiled"
        self.root.mkdir(); self.compiled.mkdir()
        self.source = "# café stays intact\nfrom harnest.graph import Graph, Edge, START\ndef respond(value): return value\nhelper = Agent(name='helper', model='test')\nroot_agent = Graph(nodes={'respond': respond, 'helper': helper}, edges=[Edge(START, 'respond')], description='Before')\n"
        for root in (self.root, self.compiled):
            (root / "agent.py").write_text(self.source)
            (root / "instructions.md").write_text("Be clear.")
        self.studio = PlaygroundStudioService(self.compiled, mode="managed")
        self.client = self.make_client()

    def make_client(self, writable=True, host="127.0.0.1"):
        """Install the production routes with explicit development ownership."""

        app = FastAPI()
        with authoring_workspace(self.root if writable else None):
            install_builder_routes(app.router, self.studio)
        client = TestClient(app, base_url="http://127.0.0.1", client=(host, 1234))
        self.addCleanup(client.close)
        return client

    def read(self, path):
        """Read the same source revision a browser must present before saving."""

        response = self.client.get("/_harnest/authoring/document", params={"path": path})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def write(self, document, text):
        """Submit text through the real revision-checked write endpoint."""

        return self.client.put("/_harnest/authoring/document", json={**document, "text": text})

    def test_source_save_preserves_artifact_and_rejects_stale_edits(self):
        """Writes affect only authoring source and cannot overwrite concurrent editor changes."""

        document = self.read("instructions.md")
        self.assertEqual(self.write(document, "Updated.").status_code, 200)
        self.assertEqual((self.root / "instructions.md").read_text(), "Updated.")
        self.assertEqual((self.compiled / "instructions.md").read_text(), "Be clear.")
        self.assertEqual(self.write(document, "Stale.").status_code, 409)

    def test_new_suite_validates_ids_and_preserves_advanced_case_fields(self):
        """New files can be created, while duplicate IDs and invalid schemas cannot be saved."""

        document = self.read("evals/quality.evalset.json")
        payload = {"eval_set_id": "quality", "name": "Quality", "eval_cases": [{"evalId": "hello", "conversation": [{"userContent": {"role": "user", "parts": [{"text": "hi"}]}, "finalResponse": {"role": "model", "parts": [{"text": "hello"}]}, "intermediateData": {"toolUses": [{"name": "lookup", "args": {"query": "hi"}}]}}]}]}
        response = self.write(document, json.dumps(payload))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(json.loads((self.root / document["path"]).read_text()), payload)
        stale = response.json()
        payload["eval_cases"].append(payload["eval_cases"][0])
        self.assertEqual(self.write(stale, json.dumps(payload)).status_code, 422)
        self.assertEqual(self.write(document, "{}").status_code, 422)

    def test_invalid_source_never_replaces_previous_file(self):
        """Syntax validation happens before an atomic write becomes visible to the watcher."""

        document = self.read("agent.py")
        self.assertEqual(self.write(document, "def broken(").status_code, 422)
        self.assertEqual((self.root / "agent.py").read_text(), self.source)

    def test_source_routes_reject_escape_hidden_files_and_linked_parents(self):
        """Read/create share path constraints even when the target does not exist."""

        for path in ("../outside.py", "/tmp/outside.py", ".env", "evals/../../outside.py", "other.py"):
            response = self.client.get("/_harnest/authoring/document", params={"path": path})
            self.assertEqual(response.status_code, 404, path)
        (self.root / "evals").symlink_to(self.compiled, target_is_directory=True)
        self.assertEqual(self.client.get("/_harnest/authoring/document", params={"path": "evals/new.evalset.json"}).status_code, 404)

    def test_read_only_launchers_and_remote_origins_cannot_edit(self):
        """Assets alone grant no write authority, and all routes enforce local origin checks."""

        body = {"path": "instructions.md", "revision": "", "text": "changed"}
        for client in (self.make_client(False), self.make_client(host="192.0.2.1")):
            self.assertEqual(client.put("/_harnest/authoring/document", json=body).status_code, 403)
        for headers in ({"Origin": "https://elsewhere.test"}, {"Sec-Fetch-Site": "cross-site"}, {"Host": "elsewhere.test"}):
            self.assertEqual(self.client.put("/_harnest/authoring/document", json=body, headers=headers).status_code, 403)

    def test_graph_form_preserves_surrounding_source_and_validates_endpoints(self):
        """Form edits preserve comments and reject missing nodes before any write."""

        document = self.read("agent.py")
        value = self.client.get("/_harnest/authoring/form", params={"path": "agent.py", "line": 5}).json()
        self.assertEqual(value["workflow"]["nodes"], {"respond": "respond", "helper": "helper"})
        body = {"path": "agent.py", "revision": document["revision"], "line": 5, "fields": {"description": "After"}, "workflow": {"nodes": {"respond": "respond"}, "edges": [{"source": "START", "target": "missing"}]}}
        self.assertEqual(self.client.put("/_harnest/authoring/form", json=body).status_code, 422)
        body["workflow"]["edges"][0]["target"] = "respond"
        response = self.client.put("/_harnest/authoring/form", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue((self.root / "agent.py").read_text().startswith("# café stays intact\nfrom"))
        ast.parse((self.root / "agent.py").read_text())

    def test_mcp_creation_selection_and_removal_are_scope_owned(self):
        """Factories use credential references, support filtered tools, and require a removal revision."""

        body = {"name": "catalog", "endpoint": "http://localhost:8000/mcp", "token_env": "CATALOG_TOKEN", "tools": ["lookup"]}
        response = self.client.post("/_harnest/authoring/mcp", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("os.environ['CATALOG_TOKEN']", response.json()["text"])
        self.assertEqual(self.client.post("/_harnest/authoring/mcp", json=body).status_code, 409)
        form = self.client.get("/_harnest/authoring/form", params={"path": "mcp/catalog.py", "line": 4})
        self.assertEqual(form.status_code, 200, form.text)
        self.assertEqual(form.json()["fields"]["tools"], ["lookup"])
        wrong = {"path": "instructions.md", "revision": self.read("instructions.md")["revision"]}
        self.assertEqual(self.client.request("DELETE", "/_harnest/authoring/mcp", json=wrong).status_code, 422)
        delete = {"path": "mcp/catalog.py", "revision": response.json()["revision"]}
        self.assertEqual(self.client.request("DELETE", "/_harnest/authoring/mcp", json=delete).status_code, 200)
        self.assertFalse((self.root / "mcp/catalog.py").exists())

    def test_connections_cannot_target_other_agent_scopes(self):
        """A browser cannot choose arbitrary folders by forging a resource scope."""

        response = self.client.post("/_harnest/authoring/mcp", json={"scope": "../outside", "name": "catalog", "endpoint": "http://localhost/mcp"})
        self.assertEqual(response.status_code, 422)

    def test_generated_stdio_factory_uses_the_real_constructor_contract(self):
        """Arguments round-trip through MCPClient without starting a subprocess."""

        from harnest.playground_authoring import mcp_source
        namespace = {}
        exec(mcp_source("catalog", "stdio", "python", ["server.py"], [], ""), namespace)
        configured = namespace["client"]()
        self.assertEqual(configured.command, "python")
        self.assertEqual(tuple(configured.args), ("server.py",))
        self.assertEqual(configured.tool_filter, [])
