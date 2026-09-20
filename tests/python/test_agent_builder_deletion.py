"""HTTP and filesystem coverage for reversible Studio capability removal."""

import json
from pathlib import Path
from unittest.mock import patch

from test_agent_builder import _BuilderFixture


class BuilderDeletionTests(_BuilderFixture):
    """Delete exact source capabilities while preserving recovery, revisions, and unrelated files."""

    def create_tool(self):
        """Create a discovered resource without requiring a native CLI subprocess."""
        (self.project / "tools").mkdir(exist_ok=True)
        path = self.project / "tools/search.py"
        path.write_text('def search():\n    """Return a result."""\n    return "found"\n')
        return path

    def preview(self, path):
        """Request the same revision-bound preview shown in the inspector dialog."""
        response = self.client.post("/api/capabilities/delete-preview", json={"project": "sample", "path": path})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def remove(self, preview):
        """Submit exactly the reviewed source identity rather than trusting a new UI path."""
        return self.client.post("/api/capabilities/delete", json={"project": "sample", "path": preview["path"], "revision": preview["revision"]})

    def restore(self, identity):
        """Use the opaque recovery identity and project scope returned by deletion."""
        return self.client.post("/api/capabilities/restore", json={"project": "sample", "identity": identity})

    def test_delete_and_restore_preserves_source_permissions_and_discovery(self):
        """Soft deletion removes discovery immediately and survives a new application instance."""
        path = self.create_tool()
        original = path.read_bytes()
        path.chmod(0o640)
        preview = self.preview("tools/search.py")
        self.assertTrue(path.exists())
        self.assertFalse((self.project / ".harnest/builder-deleted").exists())
        response = self.remove(preview)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(path.exists())
        files = self.client.get("/api/project", params={"project": "sample"}).json()["files"]
        self.assertNotIn("tools/search.py", files)
        self.assertFalse(any("builder-deleted" in item for item in files))
        from harnest_builder.deleted_store import listing
        identity = listing(self.project)[0]["identity"]
        self.assertEqual(identity, response.json()["identity"])
        self.assertEqual(self.restore(identity).status_code, 200)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.client.get("/api/capabilities/deleted", params={"project": "sample"}).json(), {"items": []})
        self.assertEqual(self.restore(identity).status_code, 409)

    def test_package_entrypoint_removes_and_restores_all_supporting_files(self):
        """A skill delete preserves binary resources and hidden package files in recovery."""
        folder = self.project / "skills/research"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text("---\nname: research\ndescription: Research\n---\nRead evidence.\n")
        (folder / "reference.bin").write_bytes(bytes(range(256)))
        (folder / ".notes").write_text("local notes")
        preview = self.preview("skills/research/SKILL.md")
        self.assertEqual(preview["path"], "skills/research")
        self.assertEqual(len(preview["files"]), 3)
        result = self.remove(preview).json()
        self.assertFalse(folder.exists())
        self.assertEqual(self.restore(result["identity"]).status_code, 200)
        self.assertEqual((folder / "reference.bin").read_bytes(), bytes(range(256)))
        self.assertEqual((folder / ".notes").read_text(), "local notes")

    def test_subagent_entrypoint_includes_descendants_but_nested_tools_are_independent(self):
        """Removing a scoped capability must never silently delete its owning agent."""
        folder = self.project / "subagents/research"
        (folder / "tools").mkdir(parents=True)
        (folder / "agent.py").write_text("research = None\n")
        (folder / "instructions.md").write_text("Research")
        (folder / "tools/search.py").write_text("def search(): return 1\n")
        child = self.preview("subagents/research/tools/search.py")
        self.assertEqual(child["files"], ["subagents/research/tools/search.py"])
        parent = self.preview("subagents/research/agent.py")
        self.assertEqual(parent["path"], "subagents/research")
        self.assertEqual(len(parent["files"]), 3)

    def test_changed_source_or_references_rejects_stale_preview(self):
        """Rechecking both source and reference candidates prevents acting on an outdated review."""
        path = self.create_tool()
        preview = self.preview("tools/search.py")
        path.write_text("def search(): return 2\n")
        self.assertEqual(self.remove(preview).status_code, 409)
        preview = self.preview("tools/search.py")
        (self.project / "agent.py").write_text('root_agent = Graph(nodes={"find": "search"}, edges=[])\n')
        self.assertEqual(self.remove(preview).status_code, 409)
        current = self.preview("tools/search.py")
        self.assertIn("agent.py", current["references"])
        self.assertTrue(path.exists())

    def test_restore_never_overwrites_a_recreated_capability(self):
        """Original source remains recoverable when the destination has been reused."""
        path = self.create_tool()
        result = self.remove(self.preview("tools/search.py")).json()
        path.write_text("# replacement\n")
        self.assertEqual(self.restore(result["identity"]).status_code, 409)
        self.assertEqual(path.read_text(), "# replacement\n")
        self.assertEqual(len(self.client.get("/api/capabilities/deleted", params={"project": "sample"}).json()["items"]), 1)

    def test_core_categories_hidden_paths_and_links_are_rejected(self):
        """Deletion cannot target project roots, environment state, traversal, or linked source."""
        self.create_tool()
        (self.project / "tools/linked.py").symlink_to(self.project / "agent.py")
        for path in ("agent.py", "config.yaml", "pyproject.toml", "instructions.md", "tools/", ".harnest/state.json", "tools/../agent.py", "tools/linked.py", "../sample"):
            with self.subTest(path=path):
                response = self.client.post("/api/capabilities/delete-preview", json={"project": "sample", "path": path})
                self.assertEqual(response.status_code, 422, response.text)
        self.assertTrue((self.project / "agent.py").is_file())

    def test_package_links_and_changed_binary_assets_are_rejected(self):
        """Complete-package snapshots cover assets beyond the editable text inventory."""
        folder = self.project / "plugins/demo"
        folder.mkdir(parents=True)
        (folder / "plugin.json").write_text('{"name":"demo"}')
        asset = folder / "asset.bin"
        asset.write_bytes(b"old")
        preview = self.preview("plugins/demo")
        asset.write_bytes(b"new")
        self.assertEqual(self.remove(preview).status_code, 409)
        (folder / "linked").symlink_to(self.root)
        response = self.client.post("/api/capabilities/delete-preview", json={"project": "sample", "path": "plugins/demo"})
        self.assertEqual(response.status_code, 422)

    def test_archive_failure_keeps_original_source_and_no_recovery_record(self):
        """A failed final move leaves the authored resource intact and clears incomplete metadata."""
        path = self.create_tool()
        preview = self.preview("tools/search.py")
        with patch.object(Path, "rename", side_effect=OSError("injected failure")):
            self.assertEqual(self.remove(preview).status_code, 500)
        self.assertTrue(path.exists())
        self.assertEqual(self.client.get("/api/capabilities/deleted", params={"project": "sample"}).json(), {"items": []})

    def test_interrupted_archive_does_not_hide_other_recoverable_capabilities(self):
        """Incomplete pre-move records must not break recovery after a process restart."""
        self.create_tool()
        result = self.remove(self.preview("tools/search.py")).json()
        (self.project / ".harnest/builder-deleted" / ("a" * 32)).mkdir()
        items = self.client.get("/api/capabilities/deleted", params={"project": "sample"}).json()["items"]
        self.assertEqual([item["identity"] for item in items], [result["identity"]])

    def test_recovery_storage_links_and_tampered_paths_are_rejected(self):
        """Neither a replaced archive ancestor nor forged metadata may redirect recovery."""
        path = self.create_tool()
        result = self.remove(self.preview("tools/search.py")).json()
        entry = self.project / ".harnest/builder-deleted" / result["identity"]
        metadata = json.loads((entry / "metadata.json").read_text())
        metadata["path"] = "../outside.py"
        (entry / "metadata.json").write_text(json.dumps(metadata))
        self.assertEqual(self.restore(result["identity"]).status_code, 422)
        self.assertFalse(path.exists())
        source = entry / "source"
        source.unlink()
        source.symlink_to(self.project / "agent.py")
        self.assertEqual(self.restore(result["identity"]).status_code, 422)
        self.assertEqual(self.restore("../outside").status_code, 422)

    def test_delete_and_restore_reject_active_commands_and_preview(self):
        """Source removal and recovery cannot race a supervised build or live reload process."""
        self.create_tool()
        preview = self.preview("tools/search.py")
        result = self.remove(preview).json()
        for serving in (False, True):
            self.app.state.jobs.items["active"] = {"status": "running", "serving": serving}
            self.assertEqual(self.restore(result["identity"]).status_code, 409)
            self.app.state.jobs.items.clear()
        self.assertEqual(self.restore(result["identity"]).status_code, 200)
        for serving in (False, True):
            self.app.state.jobs.items["active"] = {"status": "running", "serving": serving}
            self.assertEqual(self.remove(preview).status_code, 409)
            self.app.state.jobs.items.clear()
