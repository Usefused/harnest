"""Keep public domain imports compatible with the existing implementation types."""

import importlib
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest


CONTRACTS = {
    "agent": {
        "agent_principal": [
            "AgentRuntimePermissionError",
            "AgentRuntimePrincipal",
        ]
    },
    "auth": {"runtime_auth": ["AuthPrincipal", "AuthenticationError", "Authenticator", "principal_for"]},
    "http": {
        "http_routes": ["AgentInvoker", "AgentResponse", "HTTPRouteError"],
        "http_lifecycle": ["HTTPCallRequest", "HTTPLifecycleContext", "HTTPLifecycleError", "HTTPResponseHead"],
    },
    "server": {"server_config": ["ServerConfig", "ServerConfigError", "ServerLimits", "load_server_config"]},
    "tool": {
        "client_tool": ["client_tool", "ClientToolError", "ClientToolExecution", "PendingClientTool", "current_transient_media"],
        "tool_lifecycle": ["ToolCallRequest", "ToolLifecycleContext", "ToolLifecycleError"],
    },
    "mcp": {"mcp_context": ["MCPContext", "MCPToolCallRequest", "MCPToolCallError", "ManagedMCPClient"]},
    "model": {"model_lifecycle": ["LiteLLMContext"], "model_hooks": ["ModelLifecycleError"]},
    "lifecycle": {"lifecycle_coverage": ["LifecycleCoverage", "CoverageLevel", "lifecycle_coverage"]},
    "context": {
        "context_session": ["SessionContext", "SessionDataError"],
        "context_storage": ["StorageContext"],
        "context_agent": ["AgentResponse", "AgentInvocationTimeout", "LocalAgentRuntime"],
        "context_assets": ["ScopedAssets"],
    },
    "runtime": {
        "runtime_contract": [
            "AgentInfo",
            "InvocationRequest",
            "InvocationResult",
            "NoCustomerFacingOutputError",
            "ResponseRequest",
            "RuntimeDriver",
            "RuntimeEvent",
            "SessionConflictError",
            "SessionMessage",
            "SessionRecord",
        ]
    },
    "sandbox": {
        "sandbox_runtime": [
            "SandboxExecutionError",
            "SandboxInputFilesUnsupportedError",
        ],
    },
    "assets": {"asset_policy": ["Stored"]},
    "store": {"storage_registry": ["CustomStorage", "StorageRegistry"]},
}


PUBLIC_API_SNAPSHOT = Path(__file__).resolve().parents[1] / "fixtures/public-api.json"


def _public_api_snapshot() -> dict[str, list[str]]:
    """Load the reviewed API surface without importing implementation paths."""

    return json.loads(PUBLIC_API_SNAPSHOT.read_text(encoding="utf-8"))


class PublicImportTests(unittest.TestCase):
    def test_public_exports_match_reviewed_snapshot(self):
        """Require every public export addition, removal, or rename to be reviewed."""

        for module_name, expected in _public_api_snapshot().items():
            module = importlib.import_module(module_name)
            exported = getattr(module, "__all__", None)
            with self.subTest(module=module_name):
                self.assertIsInstance(exported, list)
                self.assertEqual(len(exported), len(set(exported)), "duplicate export")
                self.assertTrue(all(isinstance(name, str) for name in exported))
                self.assertTrue(all(not name.startswith("_") for name in exported))
                self.assertEqual(sorted(exported), expected)
                for name in exported:
                    self.assertTrue(hasattr(module, name), f"missing export {name!r}")

    def test_public_contracts_preserve_type_and_exception_identity(self):
        """Old instances, decorators, and exception handlers survive an import migration."""
        for domain, implementations in CONTRACTS.items():
            public = importlib.import_module(f"harnest.{domain}")
            for module, names in implementations.items():
                original = importlib.import_module(f"harnest.{module}")
                for name in names:
                    with self.subTest(domain=domain, symbol=name):
                        self.assertIs(getattr(public, name), getattr(original, name))

    def test_public_exports_import_without_optional_frameworks_or_provider_sdks(self):
        """Fresh imports expose cycles and accidental optional dependencies."""
        code = '''
import importlib
import importlib.abc
import sys
class Unavailable(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('google.adk', 'langgraph', 'langchain', 'docker')):
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, Unavailable())
for name in sys.argv[1:]:
    module = importlib.import_module('harnest.' + name)
    for symbol in getattr(module, '__all__', []):
        getattr(module, symbol)
'''
        modules = [
            name.removeprefix("harnest.")
            for name in _public_api_snapshot()
            if name != "harnest"
        ]
        result = subprocess.run(
            [sys.executable, "-c", code, *modules],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_context_contracts_are_discoverable_and_unknown_names_fail(self):
        from harnest.context import AgentSession, SessionContext

        module = importlib.import_module("harnest.context")
        self.assertIn("AgentSession", dir(module))
        self.assertIn("SessionContext", module.__all__)
        with self.assertRaises(AttributeError):
            getattr(module, "UnknownPublicContract")

    def test_lifecycle_coverage_uses_domain_namespace(self):
        from harnest.lifecycle import CoverageLevel, lifecycle

        coverage = lifecycle.coverage("langgraph", "managed")
        self.assertEqual(coverage.framework, "langgraph")
        self.assertIsInstance(coverage.application, CoverageLevel)

    def test_authored_examples_do_not_recommend_helper_module_imports(self):
        """Keep public authoring examples on domain imports as new guides are added."""
        root = Path(__file__).resolve().parents[2]
        paths = list((root / "examples").rglob("*.py"))
        paths += list((root / "cmd/harnest/authoring_skill").rglob("*.md"))
        pattern = re.compile(r"(?:from|import) harnest\.[a-z]+_[a-z_]+\b")
        for path in paths:
            with self.subTest(path=str(path.relative_to(root))):
                self.assertIsNone(pattern.search(path.read_text()))
