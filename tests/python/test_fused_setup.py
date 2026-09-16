"""Provision across a real subprocess boundary and discover generated clients."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from harnest.bundle import _discover_mcp
from harnest.mcp import MCPClient
from harnest_fused import FusedCLIError, FusedMCPClient, OpenAPISpec
from harnest_fused.provision import _endpoint, _version_page


ROOT = Path(__file__).resolve().parents[2]


class FusedSetupTests(unittest.TestCase):
    def setUp(self):
        """Use a private executable and project for every provisioning attempt."""

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        self.cli = self.project / "fused-cli"
        fixture = ROOT / "tests/fixtures/fused_cli.py"
        self.cli.write_text(f"#!{sys.executable}\n" + fixture.read_text())
        self.cli.chmod(0o755)
        for name in ("crm", "billing"):
            (self.project / f"{name}.yaml").write_text("openapi: 3.0.3\ninfo: {title: Test, version: '2026-09'}\npaths: {}\n")
        self.log = self.project / "calls.jsonl"
        environment = patch.dict(os.environ, {"FUSED_TEST_LOG": str(self.log)})
        environment.start()
        self.addCleanup(environment.stop)
        self.client = FusedMCPClient.from_openapi(
            "crm.yaml", OpenAPISpec("billing.yaml", operations=["listInvoices"]),
            name="business", permission="business.use",
        )

    def setup_client(self, client=None, **options):
        """Run the public setup method with explicit infrastructure metadata."""

        return (client or self.client).setup(
            description="Find customers and their invoices.", bucket="default",
            project=self.project, cli=str(self.cli), **options,
        )

    def calls(self):
        """Read the fixture's ordered process trace."""

        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_multiple_specs_provision_one_pinned_standard_client(self):
        result = self.setup_client()
        config = json.loads(result.config_path.read_text())
        self.assertEqual(config["services"], {
            "crm": {"version": "2026-09", "select_all": True},
            "billing": {"version": "2026-09", "operations": ["listInvoices"]},
        })
        phases = [call[:2] for call in self.calls()]
        self.assertEqual(phases[1:5], [["import", "plan"], ["import", "plan"], ["import", "apply"], ["import", "apply"]])
        self.assertEqual(phases.count(["mcp", "apply"]), 1)
        self.assertEqual(phases.count(["mcp", "versions"]), 2)
        self.assertIs(type(result.client), MCPClient)
        self.assertEqual(result.url, "https://engine.test/mcp/pinned")
        self.assertEqual(result.client.permission, "business.use")
        self.assertEqual(result.client.headers["Authorization"], "Bearer ${HARNEST_FUSED_BUSINESS_TOKEN}")
        self.assertNotIn("workspace", [call[0] for call in self.calls()])

    def test_generated_file_is_discovered_without_fused_package_or_cli(self):
        result = self.setup_client()
        target = result.write_client(self.project / "mcp/business.py")
        self.assertNotIn("harnest_fused", target.read_text())
        with patch("subprocess.run", side_effect=AssertionError("runtime provisioning")):
            clients = _discover_mcp(target.parent)
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0].identity, "business")
        self.assertEqual(clients[0].url, result.url)
        self.assertEqual(clients[0].permission, "business.use")
        with self.assertRaises(FileExistsError):
            result.write_client(target)

    def test_declarative_factory_survives_harnest_discovery(self):
        directory = self.project / "mcp"
        directory.mkdir()
        (directory / "business.py").write_text(
            'from harnest_fused import FusedMCPClient\n'
            'def client():\n'
            '    return FusedMCPClient.from_openapi("not-present.yaml", name="business")\n'
        )
        with patch("subprocess.run", side_effect=AssertionError("offline")):
            client, = _discover_mcp(directory)
        self.assertIsInstance(client, FusedMCPClient)
        self.assertEqual(client.specs[0].source, "not-present.yaml")

    def test_failure_stops_without_retry_or_disclosing_captured_output(self):
        with patch.dict(os.environ, FUSED_TEST_FAIL="import apply"):
            with self.assertRaises(FusedCLIError) as raised:
                self.setup_client()
        self.assertIn("earlier changes may have committed", str(raised.exception))
        self.assertNotIn("sensitive", str(raised.exception))
        self.assertIsNone(raised.exception.__context__)
        phases = [call[:2] for call in self.calls()]
        self.assertEqual(phases.count(["import", "apply"]), 1)
        self.assertNotIn(["mcp", "apply"], phases)
        self.assertEqual(len(list(self.project.rglob("*.import.plan.json"))), 2)

    def test_bad_json_prevents_mutation_and_does_not_retain_output(self):
        with patch.dict(os.environ, FUSED_TEST_BAD_JSON="import plan"):
            with self.assertRaises(FusedCLIError) as raised:
                self.setup_client()
        self.assertIsNone(raised.exception.__context__)
        self.assertNotIn("sensitive", repr(raised.exception))
        self.assertFalse(any(call[1] == "apply" for call in self.calls()))

    def test_unknown_selection_cannot_create_an_mcp_server(self):
        client = FusedMCPClient.from_openapi(OpenAPISpec("crm.yaml", operations=["unknown"]), name="business")
        with self.assertRaisesRegex(FusedCLIError, "MCP plan"):
            self.setup_client(client)
        self.assertNotIn(["mcp", "apply"], [call[:2] for call in self.calls()])

    def test_missing_source_and_cli_fail_before_remote_work(self):
        client = FusedMCPClient.from_openapi("missing.yaml", name="business")
        with self.assertRaises(FileNotFoundError):
            self.setup_client(client)
        with self.assertRaisesRegex(FusedCLIError, "required"):
            self.client.setup(description="CRM", bucket="default", cli="/missing/fused-cli", project=self.project)
        self.assertFalse(self.log.exists())

    def test_timeout_does_not_leak_subprocess_exception_context(self):
        failure = subprocess.TimeoutExpired("fused-cli", 1, output="private-token")
        with patch("subprocess.run", side_effect=failure):
            with self.assertRaises(FusedCLIError) as raised:
                self.setup_client()
        self.assertIsNone(raised.exception.__context__)
        self.assertNotIn("private-token", str(raised.exception))

    def test_version_lookup_rejects_missing_pinned_url_and_stalled_pages(self):
        with self.assertRaises(FusedCLIError):
            _endpoint([{"status": "active", "transport_urls": {"streamable_http": "https://test/stable"}}])
        with self.assertRaises(FusedCLIError):
            _version_page({"items": [], "total": 1}, 0)

    def test_wheel_contains_standalone_package_and_typing_marker(self):
        if importlib.util.find_spec("build") is None:
            self.skipTest("quality dependencies not installed")
        destination = self.project / "dist"
        subprocess.run([sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(destination),
                        str(ROOT / "packages/harnest-fused")], check=True, capture_output=True, text=True)
        wheel, = destination.glob("*.whl")
        with zipfile.ZipFile(wheel) as archive:
            self.assertIn("harnest_fused/py.typed", archive.namelist())
            self.assertIn("harnest_fused/client.py", archive.namelist())
