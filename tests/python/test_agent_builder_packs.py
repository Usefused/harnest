"""Studio Pack parsing, reviewed source transactions, and company wheel integration."""

import asyncio
import csv
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import certifi
from zipfile import ZipFile

from fastapi import HTTPException
from fastapi.testclient import TestClient
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "studio/src"))
from harnest_builder.app import create_app
from harnest_builder.pack_distribution import package_packs
from harnest_builder.packs import Packs
from harnest_builder.prompting import Prompt, propose


class StudioPackTests(unittest.TestCase):
    """Exercise packs using real files, the authenticated API, and installable wheel artifacts."""

    def setUp(self):
        """Isolate pack publishing from the employee's mutable workspace."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pack = self.root / "pack"
        shutil.copytree(ROOT / "examples/studio-packs/company-support", self.pack)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.project = self.workspace / "existing"
        shutil.copytree(self.pack / "templates/support", self.project)
        self.packs = Packs([self.pack])
        self.app = create_app(self.workspace, "/missing/harnest", token="test", packs=self.packs)
        self.client = self.enterContext(TestClient(self.app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234)))
        self.client.headers["Authorization"] = "Bearer test"

    def preview(self, resources, **target):
        """Request the same immutable review that the local Studio UI displays."""
        response = self.client.post("/api/packs/preview", json={"resources": resources, **target})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def apply(self, review):
        """Commit the server-owned plan without accepting replacement client-side source."""
        response = self.client.post("/api/packs/apply", json={"review": review["review"]})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def change_manifest(self, change):
        """Alter publisher inputs without mutating a previously loaded catalog snapshot."""
        path = self.pack / "studio-pack.yaml"
        manifest = yaml.safe_load(path.read_text())
        change(manifest)
        path.write_text(yaml.safe_dump(manifest))

    def test_catalog_is_shared_and_discovery_never_executes_pack_code(self):
        """Company resources have one identity in the UI, builder, and packaged snapshot."""
        marker = self.root / "executed"
        source = self.pack / "components/reference.py"
        source.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        packs = Packs([self.pack])
        self.assertFalse(marker.exists())
        self.assertEqual(self.client.get("/api/workspace").json()["packs"], self.client.get("/api/packs").json()["resources"])
        self.assertEqual(packs.catalog(), packs.context()["resources"])
        self.assertTrue(packs.context()["builder_skills"])
        self.assertTrue(next(item for item in packs.catalog() if item["kind"] == "bundle")["creates_agent"])

    def test_install_is_reviewed_revision_checked_and_receipted(self):
        """Nothing writes before approval, and external edits invalidate the whole apply."""
        review = self.preview(["company-support/ticket-reference"], project="existing")
        target = self.project / "tools/reference.py"
        self.assertFalse(target.exists())
        self.apply(review)
        receipt = json.loads((self.project / "studio-packs.lock").read_text())
        item = receipt["resources"]["company-support/ticket-reference"]
        self.assertEqual(item["version"], "1.0.0")
        self.assertEqual(item["files"]["tools/reference.py"], hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertEqual(self.client.post("/api/packs/apply", json={"review": review["review"]}).status_code, 409)
        unchanged = self.preview(["company-support/ticket-reference"], project="existing")
        self.assertEqual(unchanged["files"], [])
        self.assertEqual(unchanged["kind"], "message")
        self.assertNotIn("review", unchanged)
        self.assertFalse(self.packs.reviews)
        # A version-only upgrade needs a receipt diff, but unchanged source still guards the apply.
        self.packs.resources["company-support/ticket-reference"]["version"] = "1.0.1"
        stale = self.preview(["company-support/ticket-reference"], project="existing")
        self.assertEqual([item["path"] for item in stale["files"]], ["studio-packs.lock"])
        target.write_text("# employee edit\n")
        rejected = self.client.post("/api/packs/apply", json={"review": stale["review"]})
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(target.read_text(), "# employee edit\n")
        self.assertEqual(self.client.post("/api/packs/preview", json={"project": "existing", "resources": ["company-support/ticket-reference"]}).status_code, 409)

    def test_template_bundle_composes_profile_and_preserves_other_projects(self):
        """Create a full native agent only after reviewing its composed source and configuration."""
        review = self.preview(["company-support/support-starter"], directory=str(self.workspace), name="new-agent")
        target = self.workspace / "new-agent"
        self.assertFalse(target.exists())
        result = self.apply(review)
        self.assertEqual(result["project"], "new-agent")
        config = yaml.safe_load((target / "config.yaml").read_text())
        self.assertEqual(config["spec"]["framework"]["name"], "adk")
        self.assertTrue(config["server"]["agui"])
        self.assertIn("${COMPANY_MCP_TOKEN}", (target / "mcp/company.py").read_text())
        self.assertTrue((target / "skills/company-support/SKILL.md").is_file())
        self.assertFalse((self.project / "tools/reference.py").exists())
        repeated = self.preview(["company-support/support-starter"], project="new-agent")
        self.assertEqual(repeated["files"], [])
        self.assertNotIn("review", repeated)

    def test_templates_compile_with_local_storage_and_bundled_tools(self):
        """Both a bare template and its composed starter must compile without provider calls."""
        for resource in ("support-agent", "support-starter"):
            with self.subTest(resource=resource):
                review = self.preview(["company-support/" + resource], directory=str(self.workspace), name=resource)
                self.apply(review)
                result = subprocess.run(
                    [sys.executable, "-m", "harnest.cli", "compile", str(self.workspace / resource), "--output", str(self.root / resource)],
                    env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "OPENAI_MODEL": "test-model", "OPENAI_BASE_URL": "https://models.example.invalid/v1", "COMPANY_MODEL": "test-model", "COMPANY_MODEL_URL": "https://models.example.invalid/v1", "COMPANY_MCP_URL": "https://mcp.example.invalid/mcp", "COMPANY_MCP_TOKEN": "test-only"},
                    capture_output=True, text=True, timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((self.root / resource / "harnest-manifest.json").is_file())
                card = yaml.safe_load((self.workspace / resource / "agent-card.yaml").read_text())
                self.assertTrue(card["supportedInterfaces"])
                self.assertTrue(card["skills"])

    def test_pack_boundary_rejects_links_traversal_duplicates_and_cycles(self):
        """A declarative catalog cannot widen the set of explicitly referenced source files."""
        with self.subTest("duplicate"):
            with self.assertRaisesRegex(ValueError, "Duplicate Studio pack"):
                Packs([self.pack, self.pack])
        self.change_manifest(lambda manifest: manifest["resources"][1]["files"].update({"tools/escape.py": "../outside.py"}))
        with self.assertRaises(HTTPException):
            Packs([self.pack])
        self.change_manifest(lambda manifest: manifest["resources"][1]["files"].pop("tools/escape.py"))
        source = self.pack / "components/reference.py"
        source.unlink()
        source.symlink_to(self.project / "agent.py")
        with self.assertRaises(HTTPException):
            Packs([self.pack])
        source.unlink()
        source.write_text("answer = 42\n")
        self.change_manifest(lambda manifest: manifest["resources"][-1]["includes"].append("support-starter"))
        with self.assertRaisesRegex(ValueError, "cycle"):
            Packs([self.pack])

    def test_compatibility_and_immutable_reviews(self):
        """Changing a publisher directory cannot alter already reviewed source."""
        review = self.preview(["company-support/ticket-reference"], project="existing")
        original = (self.pack / "components/reference.py").read_text()
        (self.pack / "components/reference.py").write_text("raise RuntimeError('changed after review')\n")
        self.apply(review)
        self.assertEqual((self.project / "tools/reference.py").read_text(), original)
        self.change_manifest(lambda manifest: manifest["requires"].update(studio=">=99"))
        with self.assertRaisesRegex(ValueError, "requires Studio"):
            Packs([self.pack])

    def test_builder_selects_exact_catalog_source_for_review(self):
        """The model selects IDs while the host owns source bytes and installation receipts."""
        async def completion(**options):
            context = json.loads(options["messages"][1]["content"])
            self.assertEqual(context["studio_packs"]["resources"], self.packs.catalog())
            content = json.dumps({"summary": "Use the company ticket tool", "pack_resources": ["company-support/ticket-reference"]})
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
        response = self.client.get("/api/workspace")
        self.assertEqual(response.status_code, 200)
        from harnest_builder.files import Workspace
        workspace = Workspace(self.workspace)
        workspace.packs = self.packs
        proposal = asyncio.run(propose(workspace, Prompt(project="existing", prompt="Add the company ticket tool", model="test/model"), completion))
        self.assertTrue(proposal["pack_review"])
        self.assertEqual(proposal["files"][0]["text"], (self.pack / "components/reference.py").read_text())
        self.assertFalse((self.project / "tools/reference.py").exists())
        self.apply({"review": proposal["pack_review"]})
        repeated = asyncio.run(propose(workspace, Prompt(project="existing", prompt="Add the company ticket tool", model="test/model"), completion))
        self.assertEqual(repeated["kind"], "message")
        self.assertEqual(repeated["files"], [])
        self.assertNotIn("pack_review", repeated)

    def test_company_wheel_installs_with_embedded_pack_and_exact_dependency(self):
        """Produce a real pip-installable artifact without copying unreferenced publisher files."""
        (self.pack / "private.txt").write_text("must not ship")
        wheel = package_packs([self.pack], self.root / "dist", "acme-studio", "1.0.0")
        with ZipFile(wheel) as archive:
            names = archive.namelist()
            self.assertFalse(any("private.txt" in name for name in names))
            metadata = archive.read("acme_studio-1.0.0.dist-info/METADATA").decode()
            self.assertIn("Requires-Dist: harnest-agent-builder==0.1.0", metadata)
            records = list(csv.reader(StringIO(archive.read("acme_studio-1.0.0.dist-info/RECORD").decode())))
            self.assertEqual({row[0] for row in records}, set(names))
        target = self.root / "installed"
        result = subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--no-index", "--target", str(target), str(wheel)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        embedded = Packs([target / "acme_studio_studio/packs/company-support"])
        self.assertEqual(embedded.catalog(), self.packs.catalog())
        self.assertTrue((target / "bin/acme-studio").exists())


class StudioBuilderConfigurationTests(unittest.TestCase):
    """Verify company defaults, immutable certificate delivery, and local precedence."""

    def setUp(self):
        """Reuse the pack fixture with an isolated host builder environment."""
        self.enterContext(patch.dict(os.environ, {key: value for key, value in os.environ.items()
                                                if not key.startswith("HARNEST_BUILDER_")}, clear=True))
        StudioPackTests.setUp(self)
        self.ca = Path(certifi.where()).read_text()
        # certifi includes descriptive comments; company bundles accept certificates only.
        import re
        self.ca = "\n".join(re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", self.ca, re.DOTALL)) + "\n"
        (self.pack / "ca.pem").write_text(self.ca)

    change_manifest = StudioPackTests.change_manifest

    def test_pack_defaults_survive_wheel_relocation_without_shipping_credentials(self):
        """Deliver provider settings and trust while resolving employee secrets only on launch."""
        from harnest_builder.builder_config import BuilderConfiguration
        from harnest_builder.prompting import settings
        self.change_manifest(lambda manifest: manifest.update(builder={
            "model": "openai/company-model", "api_base": "https://ai.example.test/v1",
            "api_key_env": "COMPANY_TEST_KEY", "ca_bundle": "ca.pem", "timeout": 240, "max_tokens": 8192}))
        with patch.dict(os.environ, {"COMPANY_TEST_KEY": "never-embed-this-secret"}):
            wheel = package_packs([self.pack], self.root / "dist", "configured-studio", "1.0.0")
            with ZipFile(wheel) as archive:
                self.assertFalse(any(b"never-embed-this-secret" in archive.read(name) for name in archive.namelist()))
                archive.extractall(self.root / "relocated")
            shutil.rmtree(self.pack)
            packs = Packs([self.root / "relocated/configured_studio_studio/packs/company-support"])
            config = BuilderConfiguration(packs)
            self.addCleanup(config.close)
            environment = config.environment
            self.assertEqual(environment["HARNEST_BUILDER_API_KEY"], "never-embed-this-secret")
            self.assertEqual(environment["HARNEST_BUILDER_API_BASE"], "https://ai.example.test/v1")
            self.assertEqual(Path(environment["HARNEST_BUILDER_CA_BUNDLE"]).read_text(), self.ca)
            self.assertEqual(settings(environment), {"model": "openai/company-model", "configured": True,
                                                   "custom_ca": True, "timeout": 240, "max_tokens": 8192})
            self.assertNotIn("never-embed-this-secret", json.dumps(packs.context()))
            self.assertNotIn("HARNEST_BUILDER_MODEL", os.environ)
            config.close()
            self.assertFalse(Path(environment["HARNEST_BUILDER_CA_BUNDLE"]).exists())

    def test_partial_defaults_and_explicit_local_overrides_are_app_scoped(self):
        """One settings-only company pack is sufficient; optional team packs overlay selected fields."""
        from harnest_builder.builder_config import BuilderConfiguration
        self.change_manifest(lambda manifest: manifest.update(resources=[], builder={
            "model": "openai/company", "api_base": "https://company.example/v1", "ca_bundle": "ca.pem"}))
        team = self.root / "team"
        team.mkdir()
        (team / "studio-pack.yaml").write_text(yaml.safe_dump({"apiVersion": "harnest.dev/studio-pack/v1",
            "name": "team", "title": "Team", "version": "1.0.0", "builder": {"max_tokens": 2048}}))
        packs = Packs([self.pack, team])
        with patch.dict(os.environ, {"HARNEST_BUILDER_MODEL": "openai/local", "HARNEST_BUILDER_TIMEOUT_SECONDS": "300",
                                    "HARNEST_BUILDER_CA_BUNDLE": str(self.pack / "ca.pem")}):
            config = BuilderConfiguration(packs)
            self.addCleanup(config.close)
        self.assertEqual(config.environment["HARNEST_BUILDER_MODEL"], "openai/local")
        self.assertEqual(config.environment["HARNEST_BUILDER_MAX_TOKENS"], "2048")
        self.assertEqual(config.environment["HARNEST_BUILDER_TIMEOUT_SECONDS"], "300")
        self.assertEqual(config.environment["HARNEST_BUILDER_API_BASE"], "https://company.example/v1")
        self.assertEqual(config.environment["HARNEST_BUILDER_CA_BUNDLE"], str((self.pack / "ca.pem").resolve()))
        other = BuilderConfiguration(Packs())
        self.addCleanup(other.close)
        self.assertNotIn("HARNEST_BUILDER_MODEL", other.environment)

    def test_ca_and_builder_manifest_reject_invalid_or_secret_inputs(self):
        """Never package private keys, traversal, linked trust files, or misspelled configuration."""
        cases = [{"api_key": "secret"}, {"api_base": "https://user:secret@example.test/v1"},
                 {"timeout": 0}, {"ca_bundle": "../outside.pem"}, {"ca_bundle": "bad.pem"},
                 {"ca_bundle": "linked.pem"}]
        (self.pack / "bad.pem").write_text(self.ca + "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----")
        (self.pack / "linked.pem").symlink_to(self.pack / "ca.pem")
        for settings in cases:
            with self.subTest(settings=settings):
                self.change_manifest(lambda manifest: manifest.update(builder=settings))
                with self.assertRaises((ValueError, HTTPException)):
                    Packs([self.pack])
