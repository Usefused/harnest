"""Released developer CLI and local playground MCP security boundaries."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from harnest.cli import main
from harnest.mcp_cli import _prompt_arguments
from harnest.playground_mcp import PlaygroundMCPService, install_mcp_routes


def fixture(root):
    server = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_capabilities_server.py"
    (root / "mcp").mkdir()
    (root / "mcp" / "knowledge.py").write_text(
        "from harnest.mcp import MCPClient\n"
        f"def client(): return MCPClient.stdio({sys.executable!r}, {str(server)!r})\n"
    )


class MCPCLITests(unittest.TestCase):
    def test_inspect_and_prompt_run_from_agent_folder_without_loading_a_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture(root)
            for arguments, expected in ((["inspect", "knowledge", "--json"], "serverInfo"),
                                        (["prompt", "knowledge", "summarize", "--arg", "topic=MCP"], "messages")):
                result = subprocess.run([sys.executable, "-m", "harnest.cli", "mcp", *arguments], cwd=root, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, json.loads(result.stdout))

    def test_cli_read_shares_authoring_discovery_and_reports_missing_client(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture(root)
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["mcp", "read", "knowledge", "knowledge://handbook", "--project", temporary])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["contents"][0]["text"], "# Handbook v0")

    def test_prompt_arguments_preserve_values_and_reject_ambiguity(self):
        self.assertEqual(_prompt_arguments(["topic=a=b", "optional="]), {"topic": "a=b", "optional": ""})
        for values in (["missing"], ["=value"], ["topic=a", "topic=b"]):
            with self.assertRaises(ValueError):
                _prompt_arguments(values)


class MCPPlaygroundTests(unittest.TestCase):
    def test_local_reads_are_explicit_and_remote_or_cross_origin_calls_are_denied(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture(root)
            service = PlaygroundMCPService(root, "langgraph")
            app = FastAPI()
            install_mcp_routes(app, service)
            with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
                self.assertEqual(client.get("/_harnest/mcp").json(), {"clients": ["knowledge"]})
                response = client.post("/_harnest/mcp/knowledge", json={"operation": "inspect"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn("prompts", response.json())
                with patch.object(service, "execute", new_callable=AsyncMock) as execute:
                    response = client.post("/_harnest/mcp/knowledge", json={"operation": "inspect"}, headers={"Origin": "https://evil.invalid"})
                    self.assertEqual(response.status_code, 403)
                    execute.assert_not_called()
                self.assertEqual(client.get("/_harnest/mcp", headers={"Host": "evil.invalid"}).status_code, 403)
            with TestClient(app, base_url="http://localhost", client=("192.0.2.1", 1234)) as client:
                self.assertEqual(client.get("/_harnest/mcp").status_code, 403)

    def test_mcp_routes_are_absent_without_developer_service(self):
        from harnest.playground import create_playground_router

        self.assertFalse(any(route.path.startswith("/_harnest/mcp") for route in create_playground_router().routes))
