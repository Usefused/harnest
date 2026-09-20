"""Independent builder HTTP, filesystem, CLI, and model-proposal integration tests."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

BUILDER_SOURCE = Path(__file__).resolve().parents[2] / "agent-builder" / "src"
sys.path.insert(0, str(BUILDER_SOURCE))

from harnest_builder.app import create_app
from harnest_builder.files import Workspace
from harnest_builder.prompting import Prompt, propose
from harnest_builder.workflow import add_node, connect, describe, to_graph

GRAPH = '''# Keep café and surrounding comments intact.
from harnest.agent import Agent
from harnest.graph import Graph, Edge, START

root_agent = Graph(
    nodes={"first": Agent(name="first", instruction="Answer clearly."), "second": Agent(name="second", instruction="Check the answer.")},
    edges=[Edge(START, "first")],
)
'''


class _BuilderFixture(unittest.TestCase):
    """Share temporary project setup without inheriting and rerunning unrelated tests."""

    def setUp(self):
        """Give each test independent filesystem and process ownership."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "sample"
        self.project.mkdir()
        (self.project / "config.yaml").write_text("metadata:\n  displayName: Sample\nspec:\n  framework:\n    name: adk\n    mode: managed\n")
        (self.project / "agent.py").write_text(GRAPH)
        (self.project / "instructions.md").write_text("Be useful.\n")
        self.app = create_app(self.root, "/missing/harnest", token="test-token")
        self.client = self.enterContext(TestClient(self.app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234)))
        self.client.headers["Authorization"] = "Bearer test-token"

    def document(self, path):
        """Fetch the exact revision token required for subsequent source mutation."""
        response = self.client.get("/api/file", params={"project": "sample", "path": path})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def save(self, changes):
        """Use the browser's batch save contract rather than calling storage internals."""
        return self.client.put("/api/files", json={"project": "sample", "files": changes})

    def wait_job(self, identity):
        """Wait with a deadline for a supervised command to produce a terminal state."""
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            job = next(j for j in self.app.state.jobs.list() if j["id"] == identity)
            if job["status"] != "running":
                return job
            time.sleep(.02)
        self.fail("Harnest command did not finish")


class AgentBuilderTests(_BuilderFixture):
    """Exercise the real standalone routes against temporary, explicitly owned projects."""

    def test_launch_authorization_and_same_origin(self):
        """Source and command access require both local origin and the private launch token."""
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/api/workspace").status_code, 200)
        for headers in ({"Authorization": ""}, {"Authorization": "Bearer wrong"}, {"Origin": "https://untrusted.example"}, {"Host": "untrusted.example"}, {"Sec-Fetch-Site": "cross-site"}):
            self.client.cookies.clear()
            response = self.client.get("/api/workspace", headers=headers)
            self.assertIn(response.status_code, (401, 403))
        self.assertEqual(self.client.get("/assets/files.py").status_code, 404)
        self.assertIn("frame-ancestors 'none'", self.client.get("/").headers["content-security-policy"])

    def test_browser_session_survives_refresh_without_launch_token(self):
        """An authenticated browser can reopen the plain URL and initialize its first agent."""
        response = self.client.get("/api/workspace")
        cookie = response.headers["set-cookie"]
        for flag in ("HttpOnly", "SameSite=strict", "Path=/api/", "Max-Age=2592000"):
            self.assertIn(flag, cookie)
        self.assertNotIn("test-token", cookie)
        del self.client.headers["Authorization"]
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/api/workspace").status_code, 200)
        result = self.client.post("/api/component", json={"project": "sample", "kind": "skill", "name": "session_test"})
        self.assertEqual(result.status_code, 200, result.text)
        for headers in ({"Origin": "http://127.0.0.1:9999"}, {"Sec-Fetch-Site": "cross-site"}, {"Host": "untrusted.example"}):
            self.assertEqual(self.client.get("/api/workspace", headers=headers).status_code, 403)

    def test_browser_session_is_bound_to_launch_and_port(self):
        """Cookie presence alone never authorizes a different server or a forged session."""
        self.client.get("/api/workspace")
        del self.client.headers["Authorization"]
        self.assertEqual(self.client.get("http://127.0.0.1:1941/api/workspace").status_code, 401)
        other = create_app(self.root, "/missing/harnest", token="replacement-token")
        with TestClient(other, base_url="http://127.0.0.1", client=("127.0.0.1", 1234)) as client:
            client.cookies.update(self.client.cookies)
            self.assertEqual(client.get("/api/workspace").status_code, 401)
        self.client.cookies.clear()
        self.client.cookies.set("harnest-builder-80", "forged")
        self.assertEqual(self.client.get("/api/workspace").status_code, 401)

    def test_all_palette_surfaces_are_exposed(self):
        """The independent catalogue includes code, runtime, testing, and provider capabilities."""
        catalog = self.client.get("/api/workspace").json()["catalog"]
        kinds = {item["kind"] for item in catalog}
        self.assertTrue({"tool", "subagent", "node", "mcp", "skill", "plugin", "extension", "sandbox", "task", "cron", "context", "lifecycle", "storage", "model", "library", "test", "eval", "source"} <= kinds)

    def test_revision_conflicts_and_invalid_source_preserve_original(self):
        """Invalid or stale browser edits cannot replace the user's current source."""
        original = self.document("agent.py")
        invalid = self.save([{**original, "text": "def broken("}])
        self.assertEqual(invalid.status_code, 422)
        good = self.save([{**original, "text": GRAPH + "\n# changed\n"}])
        self.assertEqual(good.status_code, 200, good.text)
        self.assertEqual(self.save([original]).status_code, 409)
        self.assertTrue((self.project / "agent.py").read_text().endswith("# changed\n"))

    def test_batch_conflict_does_not_partially_write(self):
        """Every revision is preflighted before any source member is replaced."""
        changes = [{**self.document("instructions.md"), "text": "Updated"}, {"path": "agent.py", "revision": "stale", "text": GRAPH}]
        self.assertEqual(self.save(changes).status_code, 409)
        self.assertEqual((self.project / "instructions.md").read_text(), "Be useful.\n")

    def test_failed_batch_write_rolls_back_completed_files(self):
        """A filesystem failure restores earlier files in the same proposed change set."""
        from harnest_builder.files import replace
        changes = [{**self.document("instructions.md"), "text": "Updated"}, {**self.document("agent.py"), "text": GRAPH + "\n# changed"}]

        def fail_second(path, text):
            """Fail only the second destination while allowing rollback of the first."""
            if path.name == "agent.py":
                raise OSError("disk full")
            replace(path, text)

        with patch("harnest_builder.files.replace", side_effect=fail_second):
            self.assertEqual(self.save(changes).status_code, 500)
        self.assertEqual((self.project / "instructions.md").read_text(), "Be useful.\n")

    def test_escape_hidden_symlink_and_duplicate_paths_are_rejected(self):
        """Containment checks apply to both existing and newly authored files."""
        for path in ("../outside.py", "/tmp/outside.py", ".env", ".harnest/a.py", "lib/../../outside.py", "lib\\bad.py", "lib//alias.py"):
            response = self.save([{"path": path, "text": "", "revision": ""}])
            self.assertEqual(response.status_code, 422, path)
        (self.project / "lib").symlink_to(self.root, target_is_directory=True)
        self.assertEqual(self.save([{"path": "lib/escape.py", "text": "", "revision": ""}]).status_code, 422)
        current = self.document("instructions.md")
        self.assertEqual(self.save([current, current]).status_code, 422)

    def test_components_create_native_files_and_refuse_collisions(self):
        """Non-command resources become source immediately and never replace an existing identity."""
        for kind in ("skill", "plugin", "model", "library", "test", "smoke", "storage", "sandbox", "eval"):
            body = {"project": "sample", "kind": kind, "name": "example"}
            result = self.client.post("/api/component", json=body)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertTrue((self.project / result.json()["files"][0]["path"]).is_file())
            self.assertEqual(self.client.post("/api/component", json=body).status_code, 409)

    def test_graph_node_creation_and_wiring_changes_real_source(self):
        """A visual graph node creates its module, import, and Graph.nodes entry together."""
        response = self.client.post("/api/component", json={"project": "sample", "kind": "node", "name": "reviewer"})
        self.assertEqual(response.status_code, 200, response.text)
        source = self.document("agent.py")
        self.assertIn("'reviewer': 'reviewer'", source["text"])
        self.assertTrue((self.project / "subagents/reviewer.py").is_file())
        edges = [{"source": "START", "target": "first"}, {"source": "first", "target": "reviewer", "route": "review"}]
        body = {"project": "sample", "revision": source["revision"], "edges": edges}
        self.assertEqual(self.client.put("/api/graph", json=body).status_code, 200)
        actual = describe(self.document("agent.py")["text"])
        self.assertEqual(actual["edges"][1], edges[1])
        self.assertEqual(self.client.put("/api/graph", json=body).status_code, 409)

    def test_graph_edits_preserve_unicode_and_reject_dynamic_or_missing_nodes(self):
        """Static editing cannot corrupt Unicode offsets or coerce dynamic graph expressions."""
        result = connect(GRAPH, [{"source": "START", "target": "second"}])
        self.assertTrue(result.startswith("# Keep café"))
        self.assertIn('"first": Agent', result)
        self.assertFalse(describe(GRAPH.replace('edges=[Edge(START, "first")]', "edges=dynamic_edges"))["available"])
        with self.assertRaisesRegex(Exception, "Connect existing nodes"):
            connect(GRAPH, [{"source": "missing", "target": "first"}])
        with self.assertRaisesRegex(Exception, "already exists"):
            add_node(GRAPH, "first")

    def test_cli_arguments_are_not_browser_shell_commands(self):
        """Unknown commands and option-like project names never launch a process."""
        for body in ({"action": "shell", "input": "touch /tmp/unwanted"}, {"action": "init", "name": "--help"}, {"action": "init", "name": "../escape"}, {"action": "add", "project": "sample", "kind": "unknown", "name": "example"}):
            self.assertEqual(self.client.post("/api/command", json=body).status_code, 422)
        self.assertEqual(self.app.state.jobs.list(), [])

    def test_cli_start_failure_is_visible_as_failed_job(self):
        """A missing CLI returns a failed job and never a simulated success."""
        response = self.client.post("/api/command", json={"action": "test", "project": "sample"})
        self.assertEqual(response.status_code, 200)
        job = self.wait_job(response.json()["id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("Unable to run Harnest", job["output"])

    def test_running_commands_block_saves_and_can_be_stopped(self):
        """Job cancellation releases a real process group and prevents concurrent CLI/source writes."""
        runner = self.root / "fake-harnest"
        runner.write_text(f"#!{sys.executable}\nimport time\nprint('running', flush=True)\ntime.sleep(60)\n")
        runner.chmod(0o700)
        self.app.state.jobs.cli = str(runner)
        job = self.client.post("/api/command", json={"action": "test", "project": "sample"}).json()
        self.assertEqual(self.save([self.document("instructions.md")]).status_code, 409)
        stopped = self.client.post(f"/api/jobs/{job['id']}/stop")
        self.assertEqual(stopped.status_code, 200)
        self.assertEqual(stopped.json()["status"], "stopped")
        self.assertEqual(self.save([self.document("instructions.md")]).status_code, 200)

    def test_convert_route_preserves_name_and_instructions(self):
        """Visual conversion keeps root identity and checks revisions before any source write."""
        text = 'from harnest.agent import Agent\nroot_agent = Agent(name="sample", model="test")\n'
        self.save([{**self.document("agent.py"), "text": text}])
        document = self.document("agent.py")
        body = {"project": "sample", "revision": document["revision"]}
        response = self.client.post("/api/graph/convert", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        current = self.document("agent.py")["text"]
        self.assertIn('name="sample"', current)
        self.assertIn("instruction='Be useful.\\n'", current)
        self.assertEqual(self.client.post("/api/graph/convert", json=body).status_code, 409)

class AgentBuilderFolderTests(_BuilderFixture):
    """Verify explicitly selected locations retain independent source authority."""

    def test_folder_browse_and_open_external_same_named_projects(self):
        """Folder navigation lists directories only and opening never aliases same-named agents."""
        with tempfile.TemporaryDirectory() as external:
            parent = Path(external)
            other = parent / "sample"
            other.mkdir()
            (other / "config.yaml").write_text("metadata: {displayName: External}\n")
            (other / "instructions.md").write_text("External original")
            (parent / ".hidden").mkdir()
            (parent / "secret.txt").write_text("not directory metadata")
            listing = self.client.get("/api/folders", params={"path": external})
            self.assertEqual(listing.status_code, 200)
            self.assertEqual([item["name"] for item in listing.json()["folders"]], ["sample"])
            self.assertEqual(self.client.post("/api/project/open", json={"path": external}).status_code, 422)
            opened = self.client.post("/api/project/open", json={"path": str(other)}).json()["project"]
            self.assertNotEqual(opened, "sample")
            self.assertEqual(self.client.get("/api/project", params={"project": opened}).json()["path"], str(other.resolve()))
            doc = self.client.get("/api/file", params={"project": opened, "path": "instructions.md"}).json()
            result = self.client.put("/api/files", json={"project": opened, "files": [{**doc, "text": "External changed"}]})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual((other / "instructions.md").read_text(), "External changed")
            self.assertEqual((self.project / "instructions.md").read_text(), "Be useful.\n")
            escaped = self.client.get("/api/file", params={"project": opened, "path": "../secret.txt"})
            self.assertEqual(escaped.status_code, 422)

    def test_directory_access_requires_token_and_absolute_existing_path(self):
        """The new navigation endpoints retain the local launch capability check."""
        self.assertEqual(self.client.get("/api/folders", headers={"Authorization": ""}).status_code, 401)
        self.assertEqual(self.client.post("/api/project/open", json={"path": str(self.project)}, headers={"Authorization": ""}).status_code, 401)
        for value in ("relative", str(self.root / "missing")):
            self.assertEqual(self.client.get("/api/folders", params={"path": value}).status_code, 422)
        opened = self.client.post("/api/project/open", json={"path": str(self.project)}).json()
        self.assertEqual(opened["project"], "sample")

    def test_extension_inventory_keeps_invalid_manifests_editable(self):
        """Listing extensions reads metadata without importing potentially executable code."""
        extension = self.project / "extensions" / "sample"
        extension.mkdir(parents=True)
        (extension / "extension.yaml").write_text("metadata: {name: sample, version: 1.2.3}\ncapabilities: [sandbox.provider]\n")
        (extension / "extension.py").write_text("raise RuntimeError('must never be imported')\n")
        result = self.client.get("/api/extensions", params={"project": "sample"}).json()["extensions"][0]
        self.assertEqual(result["version"], "1.2.3")
        self.assertEqual(result["capabilities"], ["sandbox.provider"])
        (extension / "extension.yaml").write_text("malformed: [")
        broken = self.client.get("/api/extensions", params={"project": "sample"}).json()["extensions"][0]
        self.assertTrue(broken["error"])
        self.assertIn("extensions/sample/extension.yaml", broken["files"])


class AgentBuilderPromptTests(_BuilderFixture):
    """Verify proposal/provider boundaries without requiring a paid model request."""

    def completion(self, files):
        """Capture the actual provider request and return deterministic complete source."""
        async def complete(**options):
            """Model responses follow the same shape as LiteLLM completion results."""
            self.provider_options = options
            content = json.dumps({"summary": "Updated instructions", "files": files})
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
        return complete

    def proposal(self, files, paths=None):
        """Run the production model workflow with a controlled provider boundary."""
        body = Prompt(project="sample", prompt="Improve the agent", model="openai/test", paths=paths or ["instructions.md"])
        return asyncio.run(propose(Workspace(self.root), body, self.completion(files)))

    def test_proposal_is_inert_until_reviewed_and_uses_captured_revision(self):
        """A model call cannot execute tools or modify files before explicit apply."""
        proposal = self.proposal([{"path": "instructions.md", "text": "Be precise."}])
        self.assertEqual((self.project / "instructions.md").read_text(), "Be useful.\n")
        self.assertNotIn("tools", self.provider_options)
        self.assertEqual(self.provider_options["model"], "openai/test")
        change = proposal["files"][0]
        self.assertEqual(change["before"], "Be useful.\n")
        (self.project / "instructions.md").write_text("External change")
        change.pop("before")
        self.assertEqual(self.save([change]).status_code, 409)

    def test_unseen_files_invalid_source_and_escapes_are_rejected(self):
        """Model output has no broader write authority than the reviewed source contract."""
        for candidate in ({"path": "agent.py", "text": "root_agent = None"}, {"path": "../escape.py", "text": ""}, {"path": "lib/new.py", "text": "def broken("}):
            with self.assertRaises(Exception):
                self.proposal([candidate])

    def test_new_model_files_can_be_reviewed_and_saved(self):
        """A model can introduce valid native capabilities without inventing a CLI operation."""
        proposal = self.proposal([{"path": "tools/search.py", "text": 'from harnest.agent import tool\n@tool\ndef search(query: str) -> str:\n    """Return a normalized query."""\n    return query.strip()\n'}])
        changes = [{k: v for k, v in item.items() if k != "before"} for item in proposal["files"]]
        self.assertEqual(self.save(changes).status_code, 200)
        self.assertTrue((self.project / "tools/search.py").is_file())

    def expanding_proposal(self, completion, **overrides):
        """Exercise automatic source expansion through the public request contract."""
        body = Prompt(project="sample", prompt="Switch memory sessions to Postgres", model="test/model",
                      paths=["instructions.md"], allow_related_source=True, **overrides)
        return asyncio.run(propose(Workspace(self.root), body, completion))

    def response(self, payload):
        """Build the provider response shape without invoking a real model."""
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))])

    def test_unseen_config_is_read_then_proposal_regenerated(self):
        """Never accept guessed config: regenerate with actual source and bind its original revision."""
        original = self.document("config.yaml")
        contexts = []

        async def complete(**options):
            """Simulate a model that initially skips the read protocol, then preserves real config."""
            context = json.loads(options["messages"][1]["content"])
            contexts.append(context)
            if len(contexts) == 1:
                return self.response({"files": [{"path": "config.yaml", "text": "invented: true"}]})
            documents = {doc["path"]: doc for doc in context["files"]}
            self.assertEqual(documents["config.yaml"]["text"], original["text"])
            return self.response({"summary": "Reviewed config", "files": [{"path": "config.yaml", "text": original["text"] + "# preserved\n"}]})

        proposal = self.expanding_proposal(complete)
        self.assertEqual(len(contexts), 2)
        self.assertTrue(contexts[0]["allow_related_source"])
        self.assertEqual(proposal["context_paths"], ["instructions.md", "config.yaml"])
        self.assertEqual(proposal["files"][0]["revision"], original["revision"])
        self.assertEqual(proposal["files"][0]["before"], original["text"])
        self.assertEqual(self.document("config.yaml"), original)

    def test_read_protocol_supports_related_storage_and_conflict_checked_apply(self):
        """Read requests can discover storage while preserving the usual review and stale-edit guard."""
        (self.project / "lifecycle").mkdir()
        (self.project / "lifecycle/storage.py").write_text("# existing storage\n")
        calls = []

        async def complete(**options):
            """Request existing source first, then edit it after an external concurrent change."""
            calls.append(options)
            if len(calls) == 1:
                return self.response({"read_files": ["lifecycle/storage.py", "config.yaml"]})
            (self.project / "lifecycle/storage.py").write_text("# newer external edit\n")
            return self.response({"files": [{"path": "lifecycle/storage.py", "text": "# proposed storage\n"}]})

        proposal = self.expanding_proposal(complete)
        self.assertEqual(proposal["files"][0]["before"], "# existing storage\n")
        changes = [{k: v for k, v in file.items() if k != "before"} for file in proposal["files"]]
        self.assertEqual(self.save(changes).status_code, 409)

    def test_related_source_requests_cannot_read_hidden_or_outside_files(self):
        """Automatic context follows the same inventory and link exclusions as manually selected source."""
        (self.project / ".env").write_text("private-value")
        (self.project / "linked.py").symlink_to(self.project / "agent.py")
        for path in (".env", "../outside.py", "linked.py"):
            async def complete(**options):
                """Ensure excluded file contents never reach the provider."""
                self.assertNotIn("private-value", options["messages"][1]["content"])
                return self.response({"read_files": [path]})
            with self.subTest(path=path), self.assertRaisesRegex(Exception, "outside the available"):
                self.expanding_proposal(complete)

    def test_related_source_reads_remain_bounded(self):
        """Stop expanding before an extra provider call when context exceeds the byte budget."""
        (self.project / "large.md").write_text("x" * 160000)
        calls = []

        async def complete(**options):
            """Ask for an allowed but oversized source file."""
            calls.append(options)
            return self.response({"read_files": ["large.md"]})

        with self.assertRaisesRegex(Exception, "160 KiB"):
            self.expanding_proposal(complete)
        self.assertEqual(len(calls), 1)

    def test_related_source_rounds_and_file_count_are_bounded(self):
        """A provider cannot turn one proposal into an unbounded project scan."""
        for index in range(25):
            (self.project / f"source{index}.md").write_text("source")
        calls = []

        async def complete(**options):
            """Keep requesting new source rather than completing the proposal."""
            calls.append(options)
            return self.response({"read_files": [f"source{len(calls)}.md"]})

        with self.assertRaisesRegex(Exception, "more source"):
            self.expanding_proposal(complete)
        self.assertEqual(len(calls), 3)

        async def too_many(**options):
            """Exceed the combined selected and additional file limit in one round."""
            return self.response({"read_files": [f"source{i}.md" for i in range(24)]})

        with self.assertRaisesRegex(Exception, "more source"):
            self.expanding_proposal(too_many)

    def test_related_source_is_opt_in_on_the_http_boundary(self):
        """Existing API clients retain selected-source-only sharing unless they explicitly enable reads."""
        async def complete(**options):
            """Propose unseen config until the caller enables related source reads."""
            return self.response({"files": [{"path": "config.yaml", "text": "# reviewed config\n"}]})

        app = create_app(self.root, "/missing/harnest", token="test-token", completion=complete)
        with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234)) as client:
            body = {"project": "sample", "prompt": "Change storage", "model": "test/model", "paths": ["instructions.md"]}
            headers = {"Authorization": "Bearer test-token"}
            self.assertEqual(client.post("/api/propose", json=body, headers=headers).status_code, 422)
            body["allow_related_source"] = True
            result = client.post("/api/propose", json=body, headers=headers)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertIn("config.yaml", result.json()["context_paths"])


class AgentBuilderCLIIntegrationTests(_BuilderFixture):
    """Exercise released user actions against this repository's actual Go CLI."""

    @classmethod
    def setUpClass(cls):
        """Build the CLI once per suite, allowing an explicit prebuilt binary for local runs."""
        cls.binary_temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.binary_temp.cleanup)
        cls.cli = os.getenv("HARNEST_BUILDER_TEST_CLI", str(Path(cls.binary_temp.name) / "harnest"))
        if not os.getenv("HARNEST_BUILDER_TEST_CLI"):
            subprocess.run(["go", "build", "-o", cls.cli, "./cmd/harnest"], cwd=Path(__file__).resolve().parents[2], check=True, capture_output=True, timeout=120)

    def test_real_init_add_compile_and_unit_tests(self):
        """Create and build a runnable project through the same HTTP actions as drag-and-drop."""
        self.app.state.jobs.cli = self.cli
        operations = [
            {"action": "init", "name": "real-agent", "framework": "adk", "profile": "minimal"},
            {"action": "add", "project": "real-agent", "kind": "tool", "name": "lookup"},
            {"action": "add", "project": "real-agent", "kind": "task", "name": "report"},
            {"action": "add", "project": "real-agent", "kind": "subagent", "name": "helper"},
            {"action": "compile", "project": "real-agent"},
            {"action": "test", "project": "real-agent"},
        ]
        with patch.dict(os.environ, {"HARNEST_PYTHON": sys.executable}):
            for body in operations:
                response = self.client.post("/api/command", json=body)
                self.assertEqual(response.status_code, 200, response.text)
                job = self.wait_job(response.json()["id"])
                self.assertEqual(job["status"], "succeeded", job["output"])
        self.assertTrue((self.root / "real-agent/tools/lookup.py").is_file())
        self.assertTrue((self.root / "real-agent/.harnest/builder/harnest-agent").is_file())

    def test_real_external_init_and_local_extension_install(self):
        """Exercise chosen save destinations and extension authoring/install against the real CLI."""
        self.app.state.jobs.cli = self.cli
        with tempfile.TemporaryDirectory() as external:
            body = {"action": "init", "name": "outside-agent", "directory": external}
            response = self.client.post("/api/command", json=body)
            self.assertEqual(response.status_code, 200, response.text)
            created = self.wait_job(response.json()["id"])
            self.assertEqual(created["status"], "succeeded", created["output"])
            identity = created["project"]
            self.assertTrue((Path(external) / "outside-agent/config.yaml").is_file())
            self.assertEqual(self.client.post("/api/command", json=body).status_code, 409)
            init = self.client.post("/api/command", json={"action": "add", "project": identity, "kind": "extension", "name": "clock"})
            self.assertEqual(init.status_code, 200, init.text)
            self.assertEqual(self.wait_job(init.json()["id"])["status"], "succeeded")
            source = str(Path(external) / "outside-agent/extensions/clock")
            install = self.client.post("/api/command", json={"action": "install-extension", "project": "sample", "name": source})
            self.assertEqual(install.status_code, 200, install.text)
            job = self.wait_job(install.json()["id"])
            self.assertEqual(job["status"], "succeeded", job["output"])
            items = self.client.get("/api/extensions", params={"project": "sample"}).json()["extensions"]
            self.assertEqual(items[0]["name"], "clock")
            self.assertTrue((self.project / "extensions/clock/extension.py").is_file())

    def test_real_langgraph_conversion_node_wiring_and_compile(self):
        """Verify discovered string references in the actual isolated Harnest compiler."""
        self.app.state.jobs.cli = self.cli
        with patch.dict(os.environ, {"HARNEST_PYTHON": sys.executable}):
            init = self.client.post("/api/command", json={"action": "init", "name": "graph-agent", "framework": "langgraph"}).json()
            self.assertEqual(self.wait_job(init["id"])["status"], "succeeded")
            root = self.root / "graph-agent"
            original = (root / "agent.py").read_text()
            graph = to_graph(original, (root / "instructions.md").read_text())
            self.assertIn('name="graph_agent"', graph)
            (root / "agent.py").write_text(graph)
            response = self.client.post("/api/component", json={"project": "graph-agent", "kind": "node", "name": "reviewer"})
            self.assertEqual(response.status_code, 200, response.text)
            connected = connect((root / "agent.py").read_text(), [{"source": "START", "target": "respond"}, {"source": "respond", "target": "reviewer"}])
            (root / "agent.py").write_text(connected)
            build = self.client.post("/api/command", json={"action": "compile", "project": "graph-agent"}).json()
            result = self.wait_job(build["id"])
            self.assertEqual(result["status"], "succeeded", result["output"])


class BuilderProvisionerTests(_BuilderFixture):
    """Studio delegates provisioning through the fixed CLI operation boundary."""

    def test_provision_actions_and_manifest_editor(self):
        """The new manifest remains editable source and deployment operations cannot inject argv."""

        for operation in ("init", "plan", "apply", "status", "stop", "remove"):
            with self.subTest(operation=operation), patch.object(self.app.state.jobs, "start", return_value={"id": "test"}) as start:
                response = self.client.post("/api/command", json={"action": "provision", "operation": operation, "project": "sample", "environment": "production"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(start.call_args.args[0], ["provision", operation, "--project", str(self.project.resolve()), "--environment", "production"])
        response = self.client.post("/api/command", json={"action": "provision", "operation": "exec", "project": "sample"})
        self.assertEqual(response.status_code, 422)
        response = self.client.post("/api/command", json={"action": "provision", "environment": "--context=other", "project": "sample"})
        self.assertEqual(response.status_code, 422)
        response = self.save([{"path": "harnest-deployment.yaml", "text": "name: sample\nservices: {}\n", "revision": ""}])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("name: sample", self.document("harnest-deployment.yaml")["text"])

    def test_revision_selection_is_forwarded_and_required_for_rollback(self):
        """History is read-only and rollback requires an explicitly selected positive revision."""

        with patch.object(self.app.state.jobs, "start", return_value={"id": "test"}) as start:
            for operation in ("plan", "rollback"):
                response = self.client.post("/api/command", json={"action": "provision", "operation": operation, "revision": 2, "project": "sample"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(start.call_args.args[0][-2:], ["--revision", "2"])
            response = self.client.post("/api/command", json={"action": "provision", "operation": "history", "project": "sample"})
            self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post("/api/command", json={"action": "provision", "operation": "rollback", "project": "sample"})
        self.assertEqual(response.status_code, 422)
        response = self.client.post("/api/command", json={"action": "provision", "operation": "apply", "revision": 2, "project": "sample"})
        self.assertEqual(response.status_code, 422)


class AgentBuilderOwnershipTests(_BuilderFixture):
    """Verify canvas reparenting changes real Harnest composition and preserves source safely."""

    def setUp(self):
        """Create a root tool and a typical flat CLI-generated subagent."""
        super().setUp()
        (self.project / "agent.py").write_text("from harnest.agent import Agent\nroot_agent = Agent(name='sample', model='test/model')\n")
        (self.project / "subagents").mkdir()
        (self.project / "subagents/helper.py").write_text("# Preserve this comment.\nfrom harnest.agent import Agent\nhelper = Agent(name='helper', model='test/model', instruction='Help.')\n")
        (self.project / "tools").mkdir()
        (self.project / "tools/lookup.py").write_text('from harnest.agent import tool\n@tool\ndef lookup(value: str) -> str:\n    """Return the supplied value."""\n    return value\n')

    def assign(self, resource="tools/lookup.py", owner="subagents/helper.py", revision=None):
        """Use exactly the revision and endpoint identities returned to the browser."""
        snapshot = self.client.get("/api/project?project=sample").json()["ownership"]
        return self.client.put("/api/ownership", json={"project": "sample", "resource": resource, "owner": owner, "revision": revision or snapshot["revision"]})

    def test_move_tool_compiles_under_receiving_subagent_and_can_move_back(self):
        """Exercise the HTTP edit through actual compiler composition, including flat conversion."""
        from harnest.bundle import compile_application
        from _session_store_fixture import write_session_store
        write_session_store(self.project)
        original = (self.project / "subagents/helper.py").read_bytes()
        result = self.assign()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse((self.project / "tools/lookup.py").exists())
        self.assertFalse((self.project / "subagents/helper.py").exists())
        self.assertEqual((self.project / "subagents/helper/agent.py").read_bytes(), original)
        compiled = compile_application(self.project, entrypoint="agent:root_agent")
        self.assertNotIn("lookup", [getattr(t, "__name__", "") for t in compiled.target.tools])
        child = next(agent for agent in compiled.target.sub_agents if agent.name == "helper")
        self.assertIn("lookup", [getattr(t, "__name__", "") for t in child.tools])
        result = self.assign("subagents/helper/tools/lookup.py", "agent.py")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue((self.project / "tools/lookup.py").is_file())

    def test_move_mcp_to_nested_agent_preserves_source(self):
        """MCP declarations use the same native scope movement without rewriting provider settings."""
        (self.project / "mcp").mkdir()
        text = "from harnest.mcp import MCPClient\ndef client():\n    return MCPClient.streamable_http('https://example.invalid/mcp')\n"
        (self.project / "mcp/docs.py").write_text(text)
        result = self.assign("mcp/docs.py")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual((self.project / "subagents/helper/mcp/docs.py").read_text(), text)
        snapshot = self.client.get("/api/project?project=sample").json()["ownership"]
        self.assertIn({"path": "subagents/helper/mcp/docs.py", "owner": "subagents/helper/agent.py", "kind": "mcp", "name": "docs"}, snapshot["resources"])

    def test_stale_source_and_destination_conflicts_preserve_originals(self):
        """Reviewed source changes and name collisions fail before converting the subagent."""
        snapshot = self.client.get("/api/project?project=sample").json()["ownership"]
        (self.project / "tools/lookup.py").write_text("# external change\n")
        self.assertEqual(self.assign(revision=snapshot["revision"]).status_code, 409)
        (self.project / "subagents/helper").mkdir()
        (self.project / "subagents/helper/unrelated.txt").write_text("keep")
        self.assertEqual(self.assign().status_code, 409)
        self.assertTrue((self.project / "subagents/helper.py").is_file())
        self.assertEqual((self.project / "subagents/helper/unrelated.txt").read_text(), "keep")

    def test_invalid_endpoints_and_location_dependent_source_are_rejected(self):
        """Only discovered scoped resources can move; linked or dynamic paths stay untouched."""
        for resource, owner in (("../outside.py", "agent.py"), ("agent.py", "subagents/helper.py"), ("tools/lookup.py", "missing.py")):
            self.assertEqual(self.assign(resource, owner).status_code, 422)
        (self.project / "tools/lookup.py").write_text("from .local import helper\n")
        self.assertEqual(self.assign().status_code, 422)
        (self.project / "tools/lookup.py").write_text("data = __file__\n")
        self.assertEqual(self.assign().status_code, 422)

    def test_failed_second_move_rolls_back_conversion_and_new_directories(self):
        """A failed capability move must not leave a half-converted flat subagent."""
        from harnest_builder import ownership
        actual = ownership._move_file

        def fail_tool(source, destination):
            """Fail only the tool publication while leaving rollback available."""
            if source.name == "lookup.py":
                raise OSError("disk full")
            actual(source, destination)

        with patch.object(ownership, "_move_file", side_effect=fail_tool):
            self.assertEqual(self.assign().status_code, 500)
        self.assertTrue((self.project / "tools/lookup.py").exists())
        self.assertTrue((self.project / "subagents/helper.py").exists())
        self.assertFalse((self.project / "subagents/helper").exists())

    def test_dynamic_roots_and_advanced_subagents_stay_source_owned(self):
        """The canvas never claims it can modify code-owned native composition."""
        (self.project / "subagents/helper.py").write_text("helper = factory()\n")
        self.assertEqual(self.assign().status_code, 422)
        (self.project / "agent.py").write_text(GRAPH)
        snapshot = self.client.get("/api/project?project=sample").json()["ownership"]
        self.assertFalse(snapshot["available"])

    def test_nested_destination_collision_and_link_do_not_overwrite_source(self):
        """Existing tools and linked capability directories remain outside a move's write set."""
        nested = self.project / "subagents/helper"
        nested.mkdir()
        (self.project / "subagents/helper.py").rename(nested / "agent.py")
        (nested / "tools").mkdir()
        target = nested / "tools/lookup.py"
        target.write_text("# Keep this other tool.\n")
        self.assertEqual(self.assign(owner="subagents/helper/agent.py").status_code, 409)
        self.assertEqual(target.read_text(), "# Keep this other tool.\n")
        target.unlink()
        (nested / "tools").rmdir()
        outside = self.root / "outside"
        outside.mkdir()
        (nested / "tools").symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.assign(owner="subagents/helper/agent.py").status_code, 422)
        self.assertTrue((self.project / "tools/lookup.py").is_file())
        self.assertEqual(list(outside.iterdir()), [])

    def test_failed_required_metadata_write_restores_all_moved_source(self):
        """The instructions contract participates in the same rollback as both file moves."""
        with patch("harnest_builder.ownership._create_file", side_effect=OSError("disk full")):
            self.assertEqual(self.assign().status_code, 500)
        self.assertTrue((self.project / "tools/lookup.py").exists())
        self.assertTrue((self.project / "subagents/helper.py").exists())
        self.assertFalse((self.project / "subagents/helper").exists())

    def skill(self):
        """Author a skill with text and binary assets that must remain one portable package."""
        path = self.project / "skills/research"
        (path / "references").mkdir(parents=True)
        (path / "SKILL.md").write_text("---\nname: research\ndescription: Research carefully.\n---\nRead references/guide.md.\n")
        (path / "references/guide.md").write_text("Keep this supporting guide.")
        (path / "diagram.png").write_bytes(b"\x89PNG\x00\x00test-binary")
        return path

    def test_skill_move_preserves_assets_and_actual_compiled_scope(self):
        """A moved package is discoverable only by its receiving agent, with assets intact."""
        from harnest.bundle import compile_application
        from _session_store_fixture import write_session_store
        from _skill_fixture import run_skill_tool
        skill = self.skill()
        original = (skill / "diagram.png").read_bytes()
        write_session_store(self.project)
        result = self.assign("skills/research")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(skill.exists())
        moved = self.project / "subagents/helper/skills/research"
        self.assertEqual((moved / "diagram.png").read_bytes(), original)
        self.assertEqual((moved / "references/guide.md").read_text(), "Keep this supporting guide.")
        compiled = compile_application(self.project, entrypoint="agent:root_agent")
        child = next(a for a in compiled.target.sub_agents if a.name == "helper")
        lookup = next(t for t in child.tools if getattr(t, "__name__", "") == "list_skills")
        catalog = json.loads(run_skill_tool(compiled, "helper", lookup))["skills"]
        self.assertEqual([item["name"] for item in catalog], ["research"])
        self.assertNotIn("list_skills", [getattr(t, "__name__", "") for t in compiled.target.tools])
        self.assertEqual(self.assign("subagents/helper/skills/research", "agent.py").status_code, 200)
        self.assertEqual((skill / "diagram.png").read_bytes(), original)

    def test_preview_is_read_only_and_checks_binary_asset_revisions(self):
        """Preview cannot convert a subagent, and later asset changes invalidate its save."""
        skill = self.skill()
        snapshot = self.client.get("/api/project?project=sample").json()["ownership"]
        body = {"project": "sample", "resources": ["skills/research", "tools/lookup.py"], "owner": "subagents/helper.py", "revision": snapshot["revision"]}
        preview = self.client.post("/api/ownership/preview", json=body)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertTrue(skill.exists())
        self.assertTrue((self.project / "subagents/helper.py").exists())
        self.assertFalse((self.project / "subagents/helper").exists())
        (skill / "diagram.png").write_bytes(b"external binary edit")
        self.assertEqual(self.client.put("/api/ownership", json=body).status_code, 409)
        self.assertTrue(skill.exists())

    def test_category_batch_moves_all_members_and_rolls_back_failure(self):
        """One failed package in a category batch restores earlier tools and flat conversion."""
        self.skill()
        from harnest_builder import ownership
        actual = ownership._move_file

        def fail_skill(source, destination):
            """Fail the package after moving the subagent and root tool."""
            if source.name == "research":
                raise OSError("disk full")
            actual(source, destination)

        snapshot = self.client.get("/api/project?project=sample").json()["ownership"]
        body = {"project": "sample", "resources": ["tools/lookup.py", "skills/research"], "owner": "subagents/helper.py", "revision": snapshot["revision"]}
        with patch.object(ownership, "_move_file", side_effect=fail_skill):
            self.assertEqual(self.client.put("/api/ownership", json=body).status_code, 500)
        self.assertTrue((self.project / "tools/lookup.py").exists())
        self.assertTrue((self.project / "skills/research/diagram.png").exists())
        self.assertTrue((self.project / "subagents/helper.py").exists())
        self.assertFalse((self.project / "subagents/helper").exists())
        self.assertEqual(self.client.put("/api/ownership", json=body).status_code, 200)

    def test_subagent_branch_moves_as_a_whole_and_rejects_cycles(self):
        """Nested capabilities remain under their agent when that entire branch is reparented."""
        self.assertEqual(self.assign().status_code, 200)
        (self.project / "subagents/reviewer.py").write_text("from harnest.agent import Agent\nreviewer = Agent(name='reviewer', model='test/model', instruction='Review.')\n")
        result = self.assign("subagents/helper", "subagents/reviewer.py")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue((self.project / "subagents/reviewer/subagents/helper/tools/lookup.py").exists())
        self.assertFalse((self.project / "subagents/helper").exists())
        for destination in ("subagents/reviewer/agent.py", "subagents/reviewer/subagents/helper/agent.py"):
            result = self.assign("subagents/reviewer", destination)
            self.assertEqual(result.status_code, 422, result.text)
            self.assertIn("itself", result.json()["detail"])

    def test_root_only_and_non_agent_drops_explain_why_they_are_invalid(self):
        """Every attempted edge gets an explicit policy result instead of a decorative rewire."""
        result = self.assign("extensions/demo", "subagents/helper.py")
        self.assertEqual(result.status_code, 422)
        self.assertIn("application-wide", result.json()["detail"])
        result = self.assign("tools/lookup.py", "tools/lookup.py")
        self.assertEqual(result.status_code, 422)
        self.assertIn("managed agent", result.json()["detail"])
        self.assertTrue((self.project / "tools/lookup.py").exists())

    def test_skill_package_symlinks_and_existing_destinations_are_rejected(self):
        """Folder moves cannot transport links or merge over an unrelated package."""
        skill = self.skill()
        outside = self.root / "outside.txt"
        outside.write_text("private")
        (skill / "link.txt").symlink_to(outside)
        self.assertEqual(self.assign("skills/research").status_code, 422)
        (skill / "link.txt").unlink()
        self.assertEqual(self.assign().status_code, 200)
        destination = self.project / "subagents/helper/skills/research"
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text("Keep this package.")
        self.assertEqual(self.assign("skills/research", "subagents/helper/agent.py").status_code, 409)
        self.assertTrue(skill.exists())
        self.assertEqual((destination / "SKILL.md").read_text(), "Keep this package.")
