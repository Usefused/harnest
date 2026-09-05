"""Portable Agent Plugin discovery and transport safety contracts."""

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import warnings

from harnest.agent_plugin_manifest import AgentPluginError, MCP_SCHEMA, PLUGIN_SCHEMA
from harnest.agent_plugin_runtime import (
    PortableMCP, installation_id, plugin_installation, portable_http_factory,
)
from harnest.mcp import MCPClient, _adk_mcp_toolset_metadata
from harnest.plugin import discover_plugins
from harnest.plugins import activate_runtime_plugins, release_runtime_plugins
from harnest.runtime_plugins import discover_application_extensions


class AgentPluginStandardTests(unittest.TestCase):
    """Exercise the public discovery boundary without launching provider processes."""

    def setUp(self):
        """Keep package files and any runtime state inside a disposable fixture."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.plugins = self.root / "application" / "plugins"
        self.data = self.root / "client-data"
        environment = patch.dict(os.environ, {"HARNEST_PLUGIN_DATA_DIR": str(self.data)})
        environment.start()
        self.addCleanup(environment.stop)

    def _write(self, path, content):
        """Create authored fixture content, never execute package Python."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _package(self, directory="package", **metadata):
        """Give folder names no authority over the manifest identity."""
        root = self.plugins / directory
        manifest = {"$schema": PLUGIN_SCHEMA, "name": "standard-example", **metadata}
        self._write(root / "plugin.json", json.dumps(manifest))
        return root

    def _servers(self, root, **servers):
        self._write(root / "mcp.json", json.dumps({"$schema": MCP_SCHEMA, "mcpServers": servers}))

    def _skill(self, root, name="lookup"):
        self._write(root / "skills" / name / "SKILL.md",
                    f"---\nname: {name}\ndescription: Look up a package fact.\n---\nRead this skill.\n")

    def _discover(self):
        """Capture diagnostics for privacy assertions without cluttering test output."""
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            result = discover_plugins(self.plugins)
        return result, "\n".join(str(item.message) for item in recorded)

    def _stdio(self, root, **fields):
        self._servers(root, worker={"type": "stdio", "command": "python", **fields})
        result, _ = self._discover()
        return result[0].mcp_clients[0]

    def test_metadata_only_package_uses_manifest_identity(self):
        self._package(directory="different-folder", version="preview", homepage="not-a-url")
        result, _ = self._discover()
        self.assertEqual([item.name for item in result], ["standard-example"])
        self.assertEqual(result[0].manifest["version"], "preview")
        self.assertEqual(result[0].mcp_clients, ())
        self.assertEqual(result[0].skill_directories, ())

    def test_skill_only_package_needs_no_mcp(self):
        root = self._package()
        self._skill(root)
        result, _ = self._discover()
        self.assertEqual(result[0].skill_directories, (root / "skills" / "lookup",))
        self.assertEqual(result[0].mcp_clients, ())

    def test_mcp_only_package_needs_no_skills(self):
        root = self._package()
        self._servers(root, remote={"type": "streamable-http", "url": "https://example.com/mcp"})
        result, _ = self._discover()
        self.assertEqual(len(result[0].mcp_clients), 1)
        self.assertEqual(result[0].skill_directories, ())
        self.assertEqual(result[0].mcp_sources, ())

    def test_unknown_fields_and_unimplemented_namespaces_do_not_execute(self):
        root = self._package(unknown="PRIVATE-VALUE", extensions={"foreign": ["anything"]})
        self._write(root / "plugin.py", "raise RuntimeError('must never execute')\n")
        self._write(root / "agents" / "poison.py", "raise RuntimeError('must never execute')\n")
        result, diagnostic = self._discover()
        self.assertEqual(len(result), 1)
        self.assertNotIn("unknown", result[0].manifest)
        self.assertNotIn("PRIVATE-VALUE", diagnostic)

    def test_standard_marker_prevents_legacy_python_activation(self):
        """A portable package must never gain Python authority from extra files."""
        root = self._package()
        self._write(root / "plugin.yaml", """apiVersion: harnest.dev/v1alpha1
kind: RuntimePlugin
metadata:
  name: package
  version: 1.0.0
runtime:
  entrypoint: plugin:plugin
capabilities: []
""")
        self._write(root / "plugin.py", "raise AssertionError('portable package Python executed')\n")
        descriptors = discover_application_extensions(self.plugins.parent)
        self.assertEqual(descriptors, ())
        try:
            self.assertEqual(activate_runtime_plugins(descriptors), ())
        finally:
            release_runtime_plugins(descriptors)
        result, _ = self._discover()
        self.assertEqual([item.name for item in result], ["standard-example"])

    def test_invalid_manifest_disables_only_its_package(self):
        root = self._package("invalid", name="Not-Valid")
        self._skill(root)
        self._package("valid", name="valid-package")
        result, diagnostic = self._discover()
        self.assertEqual([item.name for item in result], ["valid-package"])
        self.assertIn("plugin disabled", diagnostic)

    def test_invalid_manifest_shapes_are_rejected(self):
        invalid = [{"$schema": "unknown"}, {"name": "bad..name"}, {"name": "bad--name"},
                   {"name": "a" * 65}, {"version": 1}, {"keywords": [1]}, {"author": "person"}]
        for fields in invalid:
            with self.subTest(fields=fields):
                self._package(**fields)
                result, _ = self._discover()
                self.assertEqual(result, ())

    def test_malformed_duplicate_and_non_json_constants_are_rejected(self):
        root = self._package()
        documents = ['{"token":"PRIVATE-VALUE",', '{"name":"a","name":"b"}',
                     '{"name":NaN}', '[]']
        for document in documents:
            with self.subTest(document=document):
                self._write(root / "plugin.json", document)
                result, diagnostic = self._discover()
                self.assertEqual(result, ())
                self.assertNotIn("PRIVATE-VALUE", diagnostic)

    def test_invalid_skill_preserves_valid_skill_and_server(self):
        root = self._package()
        self._skill(root)
        self._write(root / "skills" / "broken" / "SKILL.md", "not frontmatter")
        self._servers(root, remote={"type": "sse", "url": "https://example.com/events"})
        result, diagnostic = self._discover()
        self.assertEqual([path.name for path in result[0].skill_directories], ["lookup"])
        self.assertEqual(len(result[0].mcp_clients), 1)
        self.assertIn("skill 'broken' skipped", diagnostic)

    def test_invalid_server_preserves_valid_server_and_skills(self):
        root = self._package()
        self._skill(root)
        self._servers(root, valid={"type": "stdio", "command": "python"},
                      unsupported={"type": "websocket", "url": "PRIVATE-VALUE"},
                      invalid={"type": "stdio", "command": ["PRIVATE-VALUE"]})
        result, diagnostic = self._discover()
        self.assertEqual([client.portable.server_name for client in result[0].mcp_clients], ["valid"])
        self.assertTrue(result[0].mcp_clients[0].identity.isidentifier())
        self.assertEqual(len(result[0].skill_directories), 1)
        self.assertNotIn("PRIVATE-VALUE", diagnostic)

    def test_invalid_mcp_document_does_not_disable_skills(self):
        root = self._package()
        self._skill(root)
        documents = ['{"token":"PRIVATE-VALUE",', '{"mcpServers":{},"mcpServers":{}}',
                     json.dumps({"$schema": "unknown", "mcpServers": {}})]
        for document in documents:
            with self.subTest(document=document):
                self._write(root / "mcp.json", document)
                result, diagnostic = self._discover()
                self.assertEqual(len(result[0].skill_directories), 1)
                self.assertEqual(result[0].mcp_clients, ())
                self.assertNotIn("PRIVATE-VALUE", diagnostic)

    def test_deep_json_recursion_stays_inside_document_boundary(self):
        """Parser recursion failure must not abort discovery of sibling packages."""
        root = self._package("invalid")
        self._package("valid", name="valid-package")
        nested = "[" * 1500 + '"PRIVATE-VALUE"' + "]" * 1500
        self._write(root / "plugin.json", '{"nested":' + nested + "}")
        result, diagnostic = self._discover()
        self.assertEqual([item.name for item in result], ["valid-package"])
        self.assertNotIn("PRIVATE-VALUE", diagnostic)

    def test_deep_mcp_json_recursion_preserves_package_skills(self):
        root = self._package()
        self._skill(root)
        nested = "[" * 1500 + '"PRIVATE-VALUE"' + "]" * 1500
        self._write(root / "mcp.json", '{"nested":' + nested + "}")
        result, diagnostic = self._discover()
        self.assertEqual(len(result[0].skill_directories), 1)
        self.assertEqual(result[0].mcp_clients, ())
        self.assertNotIn("PRIVATE-VALUE", diagnostic)

    def test_deep_skill_frontmatter_recursion_preserves_siblings(self):
        """A hostile optional skill cannot disable the package's other components."""
        root = self._package()
        self._skill(root)
        nested = "[" * 1500 + '"PRIVATE-VALUE"' + "]" * 1500
        self._write(root / "skills" / "broken" / "SKILL.md",
                    "---\nname: broken\ndescription: " + nested + "\n---\nInstructions.\n")
        self._servers(root, valid={"type": "stdio", "command": "python"})
        result, diagnostic = self._discover()
        self.assertEqual([path.name for path in result[0].skill_directories], ["lookup"])
        self.assertEqual(len(result[0].mcp_clients), 1)
        self.assertNotIn("PRIVATE-VALUE", diagnostic)

    def test_lone_surrogate_server_name_does_not_disable_siblings(self):
        root = self._package()
        servers = {"valid": {"type": "stdio", "command": "python"},
                   "\ud800": {"type": "stdio", "command": "python"}}
        self._servers(root, **servers)
        result, diagnostic = self._discover()
        self.assertEqual(len(result[0].mcp_clients), 1)
        self.assertEqual(result[0].mcp_clients[0].portable.server_name, "valid")
        self.assertIn("skipped", diagnostic)
        diagnostic.encode("utf-8")

    def test_http_requires_https_except_literal_loopback(self):
        root = self._package()
        accepted = ["https://example.com/mcp", "http://localhost/mcp", "http://127.0.0.1/mcp", "http://[::1]/mcp"]
        rejected = ["http://example.com/mcp", "http://localhost.example.com/mcp", "ftp://localhost/mcp",
                    "https://user:PRIVATE-VALUE@example.com/mcp", "https://example.com/mcp#fragment"]
        for url in accepted + rejected:
            with self.subTest(url=url):
                self._servers(root, server={"type": "streamable-http", "url": url})
                result, diagnostic = self._discover()
                self.assertEqual(len(result[0].mcp_clients), int(url in accepted))
                self.assertNotIn("PRIVATE-VALUE", diagnostic)

    def test_http_placeholders_remain_literal(self):
        root = self._package()
        url = "https://example.com/${PLUGIN_ROOT}/${SECRET}"
        headers = {"Authorization": "Bearer ${SECRET}", "X-Root": "${PLUGIN_ROOT}"}
        self._servers(root, server={"type": "streamable-http", "url": url, "headers": headers})
        result, _ = self._discover()
        with patch.dict(os.environ, {"SECRET": "must-not-expand"}):
            connection = result[0].mcp_clients[0].to_langgraph_connection()
        self.assertEqual(connection["url"], url)
        self.assertEqual(connection["headers"], headers)
        self.assertFalse(self.data.exists())

    def test_header_injection_and_case_duplicates_are_rejected(self):
        root = self._package()
        headers = [{"Authorization": "PRIVATE-VALUE\r\ninjected: yes"},
                   {"Authorization": "one", "authorization": "two"}, {"Bad Header": "value"}]
        for value in headers:
            with self.subTest(headers=value):
                self._servers(root, server={"type": "sse", "url": "https://example.com/mcp", "headers": value})
                result, diagnostic = self._discover()
                self.assertEqual(result[0].mcp_clients, ())
                self.assertNotIn("PRIVATE-VALUE", diagnostic)

    def test_http_url_whitespace_and_controls_cannot_be_normalized_away(self):
        """URL parsing must not silently repair a malformed authored endpoint."""
        root = self._package()
        invalid = [" https://example.com/mcp", "https://example.com/a b",
                   "https://exam\tple.com/mcp", "https://example.com/\nPRIVATE-VALUE",
                   "https://example.com/\rPRIVATE-VALUE", "https://example.com/\x00hidden",
                   "https://example.com/\x7fhidden", "https://example.com/#"]
        for url in invalid:
            with self.subTest(url=url):
                self._servers(root, server={"type": "streamable-http", "url": url})
                result, diagnostic = self._discover()
                self.assertEqual(result[0].mcp_clients, ())
                self.assertNotIn("PRIVATE-VALUE", diagnostic)

    def test_stdio_command_and_working_directory_cannot_escape(self):
        root = self._package()
        invalid = [{"command": "python script.py"}, {"command": "/usr/bin/python"},
                   {"command": "./../../escape"}, {"command": "${PLUGIN_ROOT}/worker"},
                   {"cwd": "./../../escape"}, {"cwd": "${PLUGIN_DATA}/../../escape"}, {"cwd": "/tmp"}]
        for fields in invalid:
            with self.subTest(fields=fields):
                self._servers(root, worker={"type": "stdio", "command": "python", **fields})
                result, _ = self._discover()
                self.assertEqual(result[0].mcp_clients, ())

    def test_stdio_relative_command_and_cwd_resolve_within_package(self):
        root = self._package()
        (root / "bin").mkdir()
        client = self._stdio(root, command="./bin/worker", cwd="${PLUGIN_ROOT}/bin")
        configuration = client._stdio_configuration()
        self.assertEqual(configuration["command"], str(root / "bin" / "worker"))
        self.assertEqual(configuration["cwd"], str(root / "bin"))
        self.assertFalse(self.data.exists())

    def test_stdio_expands_only_standard_variables_once(self):
        root = self._package(directory="literal-${PLUGIN_DATA}")
        client = self._stdio(root, args=["${PLUGIN_ROOT}/script", "${SECRET}", "$PLUGIN_ROOT"],
                             env={"COPY": "${PLUGIN_DATA}", "LITERAL": "${SECRET}"})
        with patch.dict(os.environ, {"SECRET": "must-not-expand"}):
            configuration = client._stdio_configuration()
        self.assertEqual(configuration["args"], [str(root / "script"), "${SECRET}", "$PLUGIN_ROOT"])
        self.assertEqual(configuration["env"]["PLUGIN_ROOT"], str(root))
        self.assertEqual(configuration["env"]["COPY"], configuration["env"]["PLUGIN_DATA"])
        self.assertEqual(configuration["env"]["LITERAL"], "${SECRET}")
        self.assertFalse(self.data.exists())

    def test_reserved_environment_variables_cannot_be_overridden(self):
        root = self._package()
        for name in ("PLUGIN_ROOT", "PLUGIN_DATA", "plugin_root"):
            with self.subTest(name=name):
                self._servers(root, worker={"type": "stdio", "command": "python", "env": {name: "PRIVATE-VALUE"}})
                result, diagnostic = self._discover()
                self.assertEqual(result[0].mcp_clients, ())
                self.assertNotIn("PRIVATE-VALUE", diagnostic)

    def test_discovery_and_adapter_configuration_do_not_create_state(self):
        root = self._package()
        client = self._stdio(root, cwd="${PLUGIN_DATA}")
        configuration = client._stdio_configuration()
        self.assertFalse(self.data.exists())
        client.portable.prepare()
        self.assertTrue(client.portable.data_directory().is_dir())
        self.assertEqual(configuration["cwd"], str(client.portable.data_directory()))
        self.assertEqual(client.portable.data_directory().stat().st_mode & 0o777, 0o700)

    def test_installation_scope_is_stable_across_artifact_roots(self):
        root = self._package()
        original = PortableMCP.create(root, "standard-example", "one")
        artifact = self.root / "artifact" / "plugins" / "package"
        with plugin_installation(original.scope):
            copied = PortableMCP.create(artifact, "standard-example", "two")
        unrelated = PortableMCP.create(artifact, "standard-example", "one")
        self.assertEqual(original.data_directory(), copied.data_directory())
        self.assertNotEqual(original.data_directory(), unrelated.data_directory())
        self.assertEqual(original.scope, installation_id(self.plugins.parent))
        self.assertFalse(self.data.exists())

    def test_invalid_scope_cannot_select_arbitrary_state_path(self):
        with self.assertRaises(ValueError):
            with plugin_installation("../../outside"):
                self.fail("invalid scope was accepted")

    def test_manifest_symlink_escape_disables_package(self):
        root = self._package()
        outside = self.root / "outside.json"
        self._write(outside, (root / "plugin.json").read_text())
        (root / "plugin.json").unlink()
        (root / "plugin.json").symlink_to(outside)
        result, diagnostic = self._discover()
        self.assertEqual(result, ())
        self.assertIn("outside", diagnostic)

    def test_skill_symlink_escape_preserves_independent_skills(self):
        root = self._package()
        self._skill(root)
        self._write(self.root / "outside-skill" / "SKILL.md", "PRIVATE-VALUE")
        (root / "skills" / "outside").symlink_to(self.root / "outside-skill", target_is_directory=True)
        result, diagnostic = self._discover()
        self.assertEqual([path.name for path in result[0].skill_directories], ["lookup"])
        self.assertNotIn("PRIVATE-VALUE", diagnostic)

    def test_data_symlink_escape_is_rejected_before_writes(self):
        root = self._package()
        client = self._stdio(root)
        outside = self.root / "outside"
        outside.mkdir()
        self.data.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(AgentPluginError):
            client.portable.prepare()
        self.assertEqual(list(outside.iterdir()), [])

    def test_working_directory_symlink_swap_is_rechecked_at_connection(self):
        """Discovery cannot authorize a later redirected working directory."""
        root = self._package()
        work = root / "work"
        work.mkdir()
        client = self._stdio(root, cwd="./work")
        work.rmdir()
        work.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(AgentPluginError):
            client._stdio_configuration()

    def test_adk_construction_failure_returns_disabled_toolset_with_identity(self):
        """Unsafe portable state paths disable only their provider before connection."""
        root = self._package()
        client = self._stdio(root)
        outside = self.root / "outside"
        outside.mkdir()
        self.data.symlink_to(outside, target_is_directory=True)
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            toolset = client.to_adk_toolset()
        self.assertEqual(asyncio.run(toolset.get_tools()), [])
        self.assertEqual(_adk_mcp_toolset_metadata(toolset),
                         (client.identity, client.capability_id, False))
        asyncio.run(toolset.close())
        self.assertEqual(list(outside.iterdir()), [])
        self.assertIn("unavailable", str(recorded[-1].message))

    def test_native_adk_construction_failure_still_raises(self):
        """Portable best-effort loading must not weaken authored native client errors."""
        client = MCPClient.stdio("python")
        with patch.object(MCPClient, "_adk_connection", side_effect=ValueError("invalid native connection")):
            with self.assertRaisesRegex(ValueError, "invalid native connection"):
                client.to_adk_toolset()

    def test_http_adapter_blocks_cross_origin_without_sending_requests(self):
        """SSE endpoint changes cannot forward package headers to another origin."""
        import httpx

        async def verify():
            async with portable_http_factory("https://example.com/mcp")() as client:
                self.assertFalse(client.follow_redirects)
                hook = client.event_hooks["request"][0]
                await hook(httpx.Request("GET", "https://example.com/other"))
                with self.assertRaises(AgentPluginError):
                    await hook(httpx.Request("GET", "https://different.example.com/mcp"))
                with self.assertRaises(AgentPluginError):
                    await hook(httpx.Request("GET", "http://example.com/mcp"))

        asyncio.run(verify())

    def test_http_adapter_supports_environment_socks_proxies(self):
        """Portable MCP sessions inherit uppercase and lowercase SOCKS proxies."""

        import httpx

        async def verify():
            for variable in ("ALL_PROXY", "all_proxy"):
                with self.subTest(variable=variable), patch.dict(
                    os.environ,
                    {
                        variable: (
                            "socks5h://proxy-user:proxy-secret@127.0.0.1:1080"
                        )
                    },
                    clear=True,
                ):
                    client = portable_http_factory("https://example.com/mcp")()
                    self.assertIsInstance(client, httpx.AsyncClient)
                    await client.aclose()

        asyncio.run(verify())

    def test_http_adapter_redacts_proxy_construction_failures(self):
        """Portable diagnostics cannot disclose credentials from proxy errors."""

        import httpx

        with patch.object(
            httpx,
            "AsyncClient",
            side_effect=ValueError(
                "invalid socks5://proxy-user:proxy-secret@127.0.0.1:1080"
            ),
        ):
            with self.assertRaises(AgentPluginError) as caught:
                portable_http_factory("https://example.com/mcp")()

        self.assertEqual(
            str(caught.exception),
            "Agent Plugin MCP HTTP client construction failed with ValueError",
        )
        self.assertNotIn("proxy-secret", str(caught.exception))
        self.assertIsNone(caught.exception.__context__)

    def test_provider_failure_diagnostic_does_not_expose_exception_payload(self):
        root = self._package()
        client = self._stdio(root)
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            client.portable.failed(RuntimeError("PRIVATE-VALUE"))
        diagnostic = "\n".join(str(item.message) for item in recorded)
        self.assertIn("RuntimeError", diagnostic)
        self.assertIn("other components remain", diagnostic)
        self.assertNotIn("PRIVATE-VALUE", diagnostic)


if __name__ == "__main__":
    unittest.main()
