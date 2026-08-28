import tempfile
import traceback
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from harnest.approval import ApprovalPolicy
from harnest.bundle import (
    BundleDuplicateError,
    BundleExportError,
    BundleImportError,
    _call_mcp_factory,
    _discover_mcp,
    _merge_mcp_sources,
)
from harnest.mcp import MCPClient
from harnest.runtime_langgraph import _approval_policies, _mcp_connections


class MCPCompilerHardeningTests(unittest.TestCase):
    def test_factory_must_literally_declare_zero_parameters(self):
        factories = (
            lambda optional=None: optional,
            lambda *args: args,
            lambda **kwargs: kwargs,
        )
        for factory in factories:
            with self.subTest(signature=str(factory)):
                with self.assertRaisesRegex(BundleExportError, "zero parameters"):
                    _call_mcp_factory(Path("mcp/client.py"), factory)

    def test_every_factory_error_redacts_arbitrary_payloads(self):
        def key_error():
            raise KeyError("customer-secret-token")

        with self.assertRaisesRegex(BundleImportError, "failed with KeyError") as caught:
            _call_mcp_factory(Path("mcp/client.py"), key_error)
        self.assertNotIn("customer-secret-token", str(caught.exception))
        self.assertNotIn(
            "customer-secret-token",
            "".join(traceback.format_exception(caught.exception)),
        )

    def test_signature_error_also_redacts_arbitrary_payloads(self):
        class UnsafeCallable:
            @property
            def __signature__(self):
                raise KeyError("customer-secret-token")

            def __call__(self):
                return MCPClient.sse("https://mcp.test/sse")

        with self.assertRaisesRegex(
            BundleImportError, "signature failed with KeyError"
        ) as caught:
            _call_mcp_factory(Path("mcp/client.py"), UnsafeCallable())
        self.assertNotIn("customer-secret-token", str(caught.exception))
        self.assertNotIn(
            "customer-secret-token",
            "".join(traceback.format_exception(caught.exception)),
        )

    def test_discovery_assigns_stable_scoped_capability_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = (
                "from harnest.mcp import MCPClient\n"
                "def client(): return MCPClient.sse('https://mcp.test/sse')\n"
            )
            direct = root / "mcp"
            plugin = root / "plugins" / "support" / "mcp"
            direct.mkdir(parents=True)
            plugin.mkdir(parents=True)
            (direct / "github.py").write_text(source, encoding="utf-8")
            (plugin / "github.py").write_text(source, encoding="utf-8")
            direct_client = _discover_mcp(direct)[0]
            plugin_client = _discover_mcp(
                plugin, capability_scope="plugin__support__mcp"
            )[0]
        self.assertEqual(direct_client.identity, "github")
        self.assertEqual(direct_client.capability_id, "mcp__github")
        self.assertEqual(
            plugin_client.capability_id, "plugin__support__mcp__github"
        )
        connections, _names = _mcp_connections((direct_client, plugin_client))
        self.assertEqual(len(connections), 2)

    def test_duplicate_configuration_ignores_compiler_policy_metadata(self):
        client = MCPClient.sse("https://mcp.test/sse")
        first = replace(
            client,
            identity="github",
            capability_id="mcp__github",
            approval=ApprovalPolicy(message="Approve?"),
        )
        second = replace(
            client,
            identity="other",
            capability_id="plugin__support__mcp__other",
            approval=None,
        )
        with self.assertRaisesRegex(BundleDuplicateError, "duplicate MCP"):
            _merge_mcp_sources(("direct", (first,)), ("plugin", (second,)))


class MCPApprovalDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_adk_selective_approval_rejects_unknown_remote_tool(self):
        policy = ApprovalPolicy(message="Approve?", tools=("missing",))
        client = replace(
            MCPClient.sse("https://mcp.test/sse"),
            identity="github",
            capability_id="mcp__github",
            approval=policy,
        )

        class Toolset:
            def __init__(self, **_kwargs):
                pass

            async def get_tools(self, _readonly_context=None):
                return [SimpleNamespace(name="merge", run_async=lambda **_kwargs: None)]

        guarded = client._adk_toolset_type(Toolset)
        with self.assertRaisesRegex(ValueError, "unavailable tools: missing"):
            await guarded().get_tools()

    async def test_langgraph_selective_approval_rejects_unknown_remote_tool(self):
        policy = ApprovalPolicy(message="Approve?", tools=("missing",))
        configured = replace(
            MCPClient.sse("https://mcp.test/sse"),
            capability_id="mcp__github",
            approval=policy,
        )
        tools = [SimpleNamespace(name="mcp__github_merge")]
        with self.assertRaisesRegex(ValueError, "unavailable tools: missing"):
            _approval_policies(tools, "mcp__github", configured)


if __name__ == "__main__":
    unittest.main()
