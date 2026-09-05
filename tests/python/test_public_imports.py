"""Keep public domain imports compatible with the existing implementation types."""

import importlib
import inspect
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


def _defined_public_callables(value: type) -> list[tuple[str, object]]:
    """Return methods and properties authored directly on a public class."""

    callables: list[tuple[str, object]] = []
    for name, descriptor in value.__dict__.items():
        if name.startswith("_"):
            continue
        if isinstance(descriptor, (staticmethod, classmethod)):
            function = descriptor.__func__
        elif isinstance(descriptor, property):
            function = descriptor.fget
        else:
            function = descriptor if inspect.isfunction(descriptor) else None
        if function is not None:
            callables.append((name, function))
    return callables


def _annotation_gaps(function: object) -> list[str]:
    """List parameters and returns that provide no IDE-visible annotation."""

    signature = inspect.signature(function)
    missing = [
        parameter.name
        for parameter in signature.parameters.values()
        if parameter.name not in {"self", "cls"}
        and parameter.annotation is inspect.Signature.empty
    ]
    if signature.return_annotation is inspect.Signature.empty:
        missing.append("return")
    return missing


def _has_authored_class_docstring(value: type) -> bool:
    """Distinguish authored hover help from a dataclass-generated signature."""

    documentation = (value.__doc__ or "").strip()
    return bool(documentation) and not documentation.startswith(f"{value.__name__}(")


def _public_harnest_objects() -> list[tuple[str, object]]:
    """Load each distinct Harnest-owned object in the reviewed public API."""

    values: list[tuple[str, object]] = []
    seen: set[int] = set()
    for module_name, exports in _public_api_snapshot().items():
        module = importlib.import_module(module_name)
        for export in exports:
            value = getattr(module, export)
            owner = getattr(value, "__module__", "")
            if id(value) in seen or not owner.startswith("harnest"):
                continue
            seen.add(id(value))
            values.append((owner, value))
    return values


def _callable_ide_gaps(
    qualified: str, function: object
) -> tuple[list[str], list[str]]:
    """Return documentation and annotation defects for one public callable."""

    missing_docs = (
        [] if (getattr(function, "__doc__", None) or "").strip() else [qualified]
    )
    gaps = _annotation_gaps(function)
    missing_annotations = [] if not gaps else [f"{qualified}: {', '.join(gaps)}"]
    return missing_docs, missing_annotations


def _initializer_annotation_gaps(owner: str, value: type) -> list[str]:
    """Check an authored or generated constructor while skipping protocol shims."""

    initializer = value.__dict__.get("__init__")
    if not inspect.isfunction(initializer) or initializer.__module__ != owner:
        return []
    gaps = _annotation_gaps(initializer)
    if not gaps:
        return []
    return [f"{owner}.{value.__qualname__}.__init__: {', '.join(gaps)}"]


def _class_ide_gaps(owner: str, value: type) -> tuple[list[str], list[str]]:
    """Return hover and signature defects for a public class surface."""

    missing_docs = (
        []
        if _has_authored_class_docstring(value)
        else [f"{owner}.{value.__qualname__}"]
    )
    missing_annotations = _initializer_annotation_gaps(owner, value)
    for name, function in _defined_public_callables(value):
        callable_docs, callable_annotations = _callable_ide_gaps(
            f"{owner}.{value.__qualname__}.{name}", function
        )
        missing_docs.extend(callable_docs)
        missing_annotations.extend(callable_annotations)
    return missing_docs, missing_annotations


def _object_ide_gaps(owner: str, value: object) -> tuple[list[str], list[str]]:
    """Dispatch public functions and classes to their IDE contract checks."""

    if inspect.isfunction(value):
        return _callable_ide_gaps(f"{owner}.{value.__qualname__}", value)
    if inspect.isclass(value):
        return _class_ide_gaps(owner, value)
    return [], []


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

    def test_public_callables_have_hover_docs_and_complete_signatures(self):
        """Keep IDE documentation and types complete across the reviewed API."""

        missing_docs: list[str] = []
        missing_annotations: list[str] = []
        for owner, value in _public_harnest_objects():
            object_docs, object_annotations = _object_ide_gaps(owner, value)
            missing_docs.extend(object_docs)
            missing_annotations.extend(object_annotations)

        self.assertEqual(missing_docs, [], "public hover documentation is incomplete")
        self.assertEqual(
            missing_annotations,
            [],
            "public callable annotations are incomplete",
        )

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
