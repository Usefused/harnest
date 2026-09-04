"""Keep public domain imports compatible with the existing implementation types."""

import importlib
from pathlib import Path
import re
import subprocess
import sys
import unittest


CONTRACTS = {
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
    "runtime": {"runtime_contract": ["ResponseRequest", "RuntimeDriver", "InvocationRequest", "InvocationResult"]},
    "assets": {"asset_policy": ["Stored"]},
    "store": {"storage_registry": ["CustomStorage", "StorageRegistry"]},
}


class PublicImportTests(unittest.TestCase):
    def test_public_contracts_preserve_type_and_exception_identity(self):
        """Old instances, decorators, and exception handlers survive an import migration."""
        for domain, implementations in CONTRACTS.items():
            public = importlib.import_module(f"harnest.{domain}")
            for module, names in implementations.items():
                original = importlib.import_module(f"harnest.{module}")
                for name in names:
                    with self.subTest(domain=domain, symbol=name):
                        self.assertIs(getattr(public, name), getattr(original, name))

    def test_public_exports_import_without_optional_frameworks(self):
        """Fresh-process imports expose cycles and accidental framework dependencies."""
        code = '''
import importlib
import importlib.abc
import sys
class Unavailable(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('google.adk', 'langgraph', 'langchain')):
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, Unavailable())
for name in sys.argv[1:]:
    module = importlib.import_module('harnest.' + name)
    for symbol in getattr(module, '__all__', []):
        getattr(module, symbol)
'''
        result = subprocess.run([sys.executable, "-c", code, *reversed(CONTRACTS)], capture_output=True, text=True, timeout=30)
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
