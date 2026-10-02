"""Studio host composition, isolated assets, recovery, and distribution integration."""

import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from zipfile import ZipFile

from fastapi import HTTPException
from fastapi.testclient import TestClient
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "studio/src"))
from harnest_builder.app import create_app
from harnest_builder.pack_distribution import package_packs
from harnest_builder.packs import Packs
from harnest_builder.ui_packs import DEFAULT_ROOT, UIPacks


class StudioUIPackTests(unittest.TestCase):
    """Test real snapshots and HTTP policy rather than loader implementation details."""

    def setUp(self):
        """Isolate published UI files from the employee's workspace."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pack = self.root / "blueprint"
        shutil.copytree(ROOT / "examples/studio-packs/blueprint-ui", self.pack)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def client(self, packs=None, **options):
        """Exercise the authenticated loopback HTTP boundary with normal lifespan cleanup."""
        app = create_app(self.workspace, "/missing/harnest", token="test", packs=packs, **options)
        client = self.enterContext(TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234)))
        client.headers["Authorization"] = "Bearer test"
        return client

    def manifest(self, change):
        """Mutate publisher inputs without changing an already loaded source snapshot."""
        path = self.pack / "studio-pack.yaml"
        value = yaml.safe_load(path.read_text())
        change(value)
        path.write_text(yaml.safe_dump(value))

    def test_default_ui_is_an_ordinary_pack_and_host_stays_independent(self):
        """The shipped shell is loaded from the shared pack contract and immutable routes."""
        standalone = Packs([DEFAULT_ROOT]).packs["fused-studio"]
        self.assertEqual(standalone["manifest"]["ui"]["apiVersion"], "harnest.dev/studio-ui/v1")
        client = self.client()
        self.assertNotIn('id="canvas-host"', client.get("/").text)
        self.assertIn('id="studio-recovery"', client.get("/").text)
        catalog = client.get("/api/ui").json()
        shell = next(item for item in catalog["contributions"] if item["slot"] == "shell")
        self.assertEqual(shell["id"], "fused-studio/shell")
        self.assertIn(standalone["digest"], shell["base"])
        source = client.get(shell["base"] + shell["entry"])
        self.assertEqual(source.status_code, 200)
        self.assertIn("export async function activate", source.text)
        self.assertEqual(client.get(shell["base"] + "assets/not-declared.js").status_code, 404)
        self.assertEqual(client.get("/assets/app.js").status_code, 404)
        self.assertFalse(catalog["themes"][0]["tokens"].get("studio-areas"))
        client.headers.clear()
        self.assertEqual(client.get("/api/ui").status_code, 401)

    def test_example_replaces_a_view_and_themes_without_installing_project_files(self):
        """Frame authority stays separate from project installation and launch credentials."""
        packs = Packs([self.pack])
        client = self.client(packs)
        catalog = client.get("/api/ui").json()
        welcome = next(item for item in catalog["contributions"] if item["slot"] == "welcome")
        self.assertEqual(welcome["id"], "blueprint-ui/welcome")
        self.assertIn("blueprint-ui/assistant-first", [item["id"] for item in catalog["layouts"]])
        response = client.get(welcome["base"] + welcome["entry"])
        self.assertEqual(response.status_code, 200)
        policy = response.headers["content-security-policy"]
        self.assertIn("sandbox allow-scripts", policy)
        self.assertIn("connect-src 'none'", policy)
        self.assertNotIn("allow-same-origin", policy)
        self.assertEqual(packs.catalog(), [])
        self.assertEqual(list(self.workspace.iterdir()), [])
        (self.pack / "assets/welcome.html").write_text("changed after review")
        self.assertEqual(client.get(welcome["base"] + welcome["entry"]).text, response.text)
        safe = client.get("/api/ui?safe=true").json()
        self.assertEqual([item["id"] for item in safe["packs"]], ["fused-studio"])

    def test_modules_require_external_trust_and_safe_launch_ignores_them(self):
        """A manifest cannot grant itself same-origin JavaScript execution."""
        (self.pack / "assets/view.js").write_text("export function activate() {}")
        def module(value):
            """Convert the example into a publisher-supplied executable view."""
            value["ui"]["assets"].append("assets/view.js")
            value["ui"]["contributions"][0].update(mode="module", entry="assets/view.js")
        self.manifest(module)
        packs = Packs([self.pack])
        untrusted = self.client(packs)
        catalog = untrusted.get("/api/ui").json()
        self.assertIn("fused-studio/welcome", [item["id"] for item in catalog["contributions"]])
        snapshot = packs.packs["blueprint-ui"]
        url = f"/ui-assets/blueprint-ui/{snapshot['digest']}/assets/view.js"
        self.assertEqual(untrusted.get(url).status_code, 403)
        trusted = self.client(packs, trusted_ui=["blueprint-ui"])
        self.assertEqual(trusted.get(url).status_code, 200)
        self.assertIn("blueprint-ui/welcome", [item["id"] for item in trusted.get("/api/ui").json()["contributions"]])
        safe = self.client(packs, trusted_ui=["blueprint-ui"], safe_ui=True)
        self.assertEqual(safe.get(url).status_code, 404)

    def test_tailwind_example_replaces_only_welcome_with_compiled_isolated_css(self):
        """A framework-built section uses existing pack boundaries without global styles or trust."""
        packs = Packs([ROOT / "examples/studio-packs/tailwind-welcome"])
        client = self.client(packs)
        catalog = client.get("/api/ui").json()
        safe = client.get("/api/ui?safe=true").json()
        remaining = [item for item in catalog["contributions"] if item["slot"] != "welcome"]
        self.assertEqual(remaining, [item for item in safe["contributions"] if item["slot"] != "welcome"])
        self.assertEqual(catalog["styles"], safe["styles"])
        self.assertEqual(catalog["themes"], safe["themes"])
        self.assertEqual(catalog["layouts"], safe["layouts"])
        welcome = next(item for item in catalog["contributions"] if item["slot"] == "welcome")
        self.assertEqual(welcome["id"], "tailwind-welcome/welcome")
        self.assertEqual(welcome["permissions"], ["state.connection"])
        response = client.get(welcome["base"] + welcome["entry"])
        self.assertEqual(response.status_code, 200)
        self.assertIn("tailwindcss v4.3.3", response.text)
        self.assertIn(".bg-slate-950", response.text)
        self.assertNotIn("STUDIO_TAILWIND_CSS", response.text)
        self.assertNotIn('<link ', response.text)
        self.assertIn("sandbox allow-scripts", response.headers["content-security-policy"])
        self.assertIn("style-src 'unsafe-inline'", response.headers["content-security-policy"])
        self.assertIn("fused-studio/welcome", [item["id"] for item in safe["contributions"]])
        self.assertEqual(packs.catalog(), [])

    def test_conflicting_replacements_fail_without_breaking_recovery(self):
        """Load order cannot resolve two claimants to the same view."""
        self.manifest(lambda value: value["ui"]["contributions"].append({**value["ui"]["contributions"][0], "id": "second"}))
        client = self.client(Packs([self.pack]))
        result = client.get("/api/ui")
        self.assertEqual(result.status_code, 422)
        self.assertIn("Conflicting", result.json()["detail"])
        self.assertEqual(client.get("/api/ui?safe=true").status_code, 200)
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            package_packs([self.pack], self.root / "dist", "conflicting-ui", "1.0.0")

    def test_bad_theme_assets_and_replacement_graphs_are_rejected(self):
        """Validation blocks CSS injection, traversal, missing assets, and cyclic ownership."""
        original = (self.pack / "studio-pack.yaml").read_text()
        changes = [
            lambda value: value["ui"]["themes"][0]["tokens"].update(accent="url(https://example.invalid)"),
            lambda value: value["ui"]["assets"].append("assets/../../outside.js"),
            lambda value: value["ui"]["assets"].append("assets/missing.js"),
            lambda value: value["ui"]["layouts"][0].update(order=["sidebar", "sidebar", "workspace"]),
        ]
        for change in changes:
            (self.pack / "studio-pack.yaml").write_text(original)
            self.manifest(change)
            with self.assertRaises((ValueError, HTTPException)):
                Packs([self.pack])
        (self.pack / "studio-pack.yaml").write_text(original)
        self.manifest(lambda value: value["ui"]["contributions"][0].update(replaces="blueprint-ui/welcome"))
        with self.assertRaisesRegex(ValueError, "Cyclic"):
            UIPacks(Packs([self.pack])).catalog()

    def test_wheel_contains_exact_ui_snapshot_and_round_trips(self):
        """Company packaging preserves frame sources and UI declarations without installing them."""
        before = Packs([self.pack]).packs["blueprint-ui"]
        wheel = package_packs([self.pack], self.root / "dist", "blueprint-team", "1.0.0")
        extracted = self.root / "installed"
        with ZipFile(wheel) as archive:
            archive.extractall(extracted)
        after = Packs([extracted / "blueprint_team_studio/packs/blueprint-ui"]).packs["blueprint-ui"]
        self.assertEqual(before["digest"], after["digest"])
        self.assertEqual(json.dumps(before["ui_assets"]), json.dumps(after["ui_assets"]))

    def test_deployment_service_replacement_requires_trust_and_recovers_default(self):
        """Replacement removes the default service alone while backend deployment remains gated."""
        packs = Packs([ROOT / "examples/studio-packs/team-deployment"])
        untrusted = self.client(packs)
        original = untrusted.get("/api/ui").json()["contributions"]
        self.assertIn("fused-studio/deployment", [item["id"] for item in original])
        trusted = self.client(packs, trusted_ui=["team-deployment"])
        replaced = trusted.get("/api/ui").json()["contributions"]
        self.assertIn("team-deployment/deployment", [item["id"] for item in replaced])
        self.assertEqual([item for item in original if item["id"] != "fused-studio/deployment"],
                         [item for item in replaced if item["id"] != "team-deployment/deployment"])
        safe = trusted.get("/api/ui?safe=true").json()["contributions"]
        self.assertIn("fused-studio/deployment", [item["id"] for item in safe])
        from unittest.mock import patch
        with patch.dict("os.environ", {"HARNEST_ENABLE_DEPLOYMENT": "false"}):
            self.assertEqual(trusted.get("/api/deployment/inspect?project=.").status_code, 403)
