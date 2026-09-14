import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import harnest.extensions as extension_namespace
from harnest.bundle import (
    BundleConventionError,
    BundleDuplicateError,
    _artifact_digest,
    compile_application,
    compile_artifact,
)
from harnest.extensions import release_extensions
from harnest.extension_descriptors import discover_extensions

from _session_store_fixture import write_session_store
from _skill_fixture import run_skill_tool


ROOT = Path(__file__).resolve().parents[2]


class HarnestExtensionCompilerIntegrationTests(unittest.TestCase):
    def test_extension_provenance_digest_matches_engine_contract(self):
        """Pin cross-language framing so Python artifacts remain Go-verifiable."""

        digest = _artifact_digest(
            [{"path": "agent.py", "sha256": "a" * 64, "size": 3}],
            [
                {
                    "name": "clock",
                    "version": "1.2.3",
                    "digest": "sha256:" + "b" * 64,
                    "requires": ["core"],
                    "capabilities": ["context.resources", "lifecycle.tool"],
                    "dependencies": ["httpx>=0.28,<1"],
                }
            ],
            interfaces={"cli": False},
        )

        self.assertEqual(
            digest,
            "sha256:23a603182458ecc7bb2f52e6c9dbeb048"
            "d0ab1fffe21bb6041160d9808415bfd",
        )

    def _write(self, path: Path, contents: str) -> None:
        """Write one authored fixture while keeping each test intent readable."""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    @staticmethod
    def _managed_backend() -> SimpleNamespace:
        """Preserve the portable target so tests can inspect compiler composition."""

        return SimpleNamespace(
            lower_managed=lambda value, **_kwargs: value,
            wrap_managed=lambda target, native_extensions=(): None,
        )

    @staticmethod
    def _advanced_backend() -> SimpleNamespace:
        """Validate the Harnest boundary without constructing a provider runtime."""

        def validate(value, fallback_name):
            return SimpleNamespace(
                name=value.name or fallback_name,
                target=value.target,
                native_app=None,
            )

        return SimpleNamespace(validate_advanced=validate)

    def _root_agent(self, root: Path, *, advanced: bool = False) -> None:
        """Create the minimum deterministic authored root accepted by the compiler."""

        write_session_store(root)
        if advanced:
            source = (
                "from harnest.agent import Agent\n"
                "root_agent = Agent.advanced(object(), name='root')\n"
            )
        else:
            source = (
                "from harnest.agent import Agent\n"
                "root_agent = Agent(name='root', model='test/model')\n"
            )
        self._write(root / "agent.py", source)
        self._write(root / "instructions.md", "Use available capabilities.\n")

    def _runtime_extension(
        self,
        root: Path,
        name: str,
        *,
        capabilities: tuple[str, ...] = (),
        requires: tuple[str, ...] = (),
        contributions: tuple[str, ...] = (),
    ) -> Path:
        """Author one strict descriptor and its public extension singleton."""

        extension = root / "extensions" / name
        requires_yaml = ""
        if requires:
            rendered = "\n".join(f"    - {item}" for item in requires)
            requires_yaml = f"requires:\n  extensions:\n{rendered}\n"
        capabilities_yaml = ""
        if capabilities:
            rendered = "\n".join(f"  - {item}" for item in capabilities)
            capabilities_yaml = f"capabilities:\n{rendered}\n"
        contributions_yaml = ""
        if contributions:
            rendered = "".join(f"  {kind}: [{kind}/]\n" for kind in contributions)
            contributions_yaml = f"contributes:\n{rendered}"
        self._write(
            extension / "extension.yaml",
            "apiVersion: harnest.dev/v1alpha1\n"
            "kind: Extension\n"
            f"metadata:\n  name: {name}\n  version: 1.2.3\n"
            "runtime:\n  entrypoint: extension:extension\n"
            f"{requires_yaml}{contributions_yaml}{capabilities_yaml}",
        )
        self._write(
            extension / "extension.py",
            "from harnest.extensions import Extension\n"
            "class AuthoredExtension(Extension):\n"
            "    pass\n"
            "extension = AuthoredExtension()\n",
        )
        return extension

    def _full_extension_content(self, extension: Path) -> None:
        """Add local tool, MCP, skill, and lifecycle contributions to a extension."""

        self._write(
            extension / "tools" / "normalize.py",
            "from harnest.agent import tool\n"
            "@tool\n"
            "def normalize(value: str) -> str:\n"
            "    \"\"\"Normalize a catalog lookup value.\"\"\"\n"
            "    return value.strip().lower()\n",
        )
        self._write(
            extension / "mcp" / "catalog.py",
            "from harnest.mcp import MCPClient\n"
            "def client():\n"
            "    return MCPClient.streamable_http(\n"
            "        'https://offline.invalid/mcp', prefix='catalog'\n"
            "    )\n",
        )
        self._write(
            extension / "skills" / "catalog-guide" / "SKILL.md",
            "---\n"
            "name: catalog-guide\n"
            "description: Explain how to use the local catalog capability.\n"
            "---\n\n"
            "# Catalog guide\n\nUse the catalog only when requested.\n",
        )
        self._write(
            extension / "lifecycle" / "audit.py",
            "from harnest import lifecycle\n"
            "@lifecycle.agent.before(order=4)\n"
            "def audit(context, value):\n"
            "    return context.next()\n",
        )

    @staticmethod
    def _release(compiled) -> None:
        """Release direct-compile acquisitions whose ownership did not reach a runtime."""

        release_extensions(
            tuple(item.descriptor for item in compiled.extensions)
        )

    def test_managed_compile_composes_runtime_extension_for_both_frameworks(self):
        capabilities = (
            "content.tools",
            "content.mcp",
            "content.skills",
            "lifecycle.agent",
        )
        for framework in ("adk", "langgraph"):
            with self.subTest(framework=framework), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self._root_agent(root)
                extension = self._runtime_extension(
                    root,
                    "temporal",
                    capabilities=capabilities,
                    contributions=("lifecycle", "mcp", "skills", "tools"),
                )
                self._full_extension_content(extension)
                compiled = None
                try:
                    with patch(
                        "harnest.bundle.get_backend",
                        return_value=self._managed_backend(),
                    ):
                        compiled = compile_application(
                            root,
                            entrypoint="agent:root_agent",
                            framework=framework,
                        )

                    tool_names = {
                        getattr(tool, "__name__", getattr(tool, "name", None))
                        for tool in compiled.target.tools
                    }
                    self.assertIn("normalize", tool_names)
                    self.assertEqual(len(compiled.target.mcp), 1)
                    self.assertEqual(
                        compiled.target.mcp[0].capability_id,
                        "extension__temporal__mcp__catalog",
                    )
                    self.assertEqual(
                        [item.descriptor.name for item in compiled.extensions],
                        ["temporal"],
                    )
                    extension_listener = next(
                        item
                        for item in compiled.lifecycle_extensions
                        if item.relative_path
                        == "extensions/temporal/lifecycle/audit.py"
                    )
                    self.assertEqual(extension_listener.phase, "before_invoke")
                    tools = {
                        tool.__name__: tool
                        for tool in compiled.target.tools
                        if hasattr(tool, "__name__")
                    }
                    catalog = json.loads(
                        run_skill_tool(compiled, "root", tools["list_skills"])
                    )["skills"]
                    self.assertEqual(
                        [(item["name"], item["description"]) for item in catalog],
                        [
                            (
                                "catalog-guide",
                                "Explain how to use the local catalog capability.",
                            )
                        ],
                    )
                finally:
                    if compiled is not None:
                        self._release(compiled)
                self.assertNotIn("harnest.extensions.temporal", sys.modules)
                self.assertFalse(hasattr(extension_namespace, "temporal"))

    def test_manifest_capabilities_gate_content_and_extensions(self):
        cases = (
            ("tools", "content.tools", "tools"),
            ("lifecycle", "lifecycle.agent", "lifecycle/audit.py"),
            ("skill_source", "lifecycle.skills", "lifecycle/skills.py"),
        )
        for contribution, capability, source in cases:
            with self.subTest(contribution=contribution), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self._root_agent(root)
                kind = "lifecycle" if contribution != "tools" else "tools"
                extension = self._runtime_extension(
                    root, "temporal", contributions=(kind,)
                )
                if contribution == "tools":
                    self._write(
                        extension / "tools" / "normalize.py",
                        "from harnest.agent import tool\n"
                        "@tool\n"
                        "def normalize(value):\n"
                        "    \"\"\"Return the supplied value unchanged.\"\"\"\n"
                        "    return value\n",
                    )
                elif contribution == "lifecycle":
                    self._write(
                        extension / "lifecycle" / "audit.py",
                        "from harnest import lifecycle\n"
                        "@lifecycle.agent.before\n"
                        "def audit(context, value): return context.next()\n",
                    )
                else:
                    self._write(
                        extension / "lifecycle" / "skills.py",
                        "from harnest import lifecycle\n"
                        "from harnest.skills import SkillSource\n"
                        "class Source(SkillSource):\n"
                        "  async def list(self, context, *, query=None, cursor=None, limit=50): pass\n"
                        "  async def load(self, skill_id, context, *, version=None): pass\n"
                        "@lifecycle.skills.source('generated')\n"
                        "def generated(): return Source()\n",
                    )

                with patch(
                    "harnest.bundle.get_backend",
                    return_value=self._managed_backend(),
                ):
                    with self.assertRaisesRegex(
                        BundleConventionError,
                        f"must declare capability '{capability}'",
                    ):
                        compile_application(
                            root,
                            entrypoint="agent:root_agent",
                            framework="langgraph",
                        )
                self.assertNotIn("harnest.extensions.temporal", sys.modules)
                self.assertFalse(hasattr(extension_namespace, "temporal"), source)

    def test_root_and_runtime_extension_tool_name_collision_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._root_agent(root)
            source = (
                "from harnest.agent import tool\n"
                "@tool\n"
                "def normalize(value):\n"
                "    \"\"\"Return the supplied value unchanged.\"\"\"\n"
                "    return value\n"
            )
            self._write(root / "tools" / "normalize.py", source)
            extension = self._runtime_extension(
                root,
                "temporal",
                capabilities=("content.tools",),
                contributions=("tools",),
            )
            self._write(extension / "tools" / "normalize.py", source)

            with patch(
                "harnest.bundle.get_backend", return_value=self._managed_backend()
            ):
                with self.assertRaisesRegex(
                    BundleDuplicateError, "duplicate tool 'normalize'"
                ):
                    compile_application(
                        root,
                        entrypoint="agent:root_agent",
                        framework="langgraph",
                    )
            self.assertNotIn("harnest.extensions.temporal", sys.modules)

    def test_custom_contribution_path_projects_without_mutating_agent_layout(self):
        """Compose declared content in place while retaining package provenance."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._root_agent(root)
            extension = self._runtime_extension(
                root,
                "temporal",
                capabilities=("content.tools",),
            )
            manifest = extension / "extension.yaml"
            manifest.write_text(
                manifest.read_text(encoding="utf-8")
                .replace(
                    "capabilities:",
                    "contributes:\n  tools: [resources/tools/]\ncapabilities:",
                ),
                encoding="utf-8",
            )
            self._write(
                extension / "resources" / "tools" / "lookup.py",
                "from harnest.agent import tool\n"
                "@tool\n"
                "def lookup(value):\n"
                "    \"\"\"Return the supplied lookup value.\"\"\"\n"
                "    return value\n",
            )
            compiled = None
            try:
                with patch(
                    "harnest.bundle.get_backend", return_value=self._managed_backend()
                ):
                    compiled = compile_application(
                        root,
                        entrypoint="agent:root_agent",
                        framework="langgraph",
                    )
                self.assertIn("lookup", {tool.__name__ for tool in compiled.target.tools})
                self.assertFalse((root / "tools" / "lookup.py").exists())
            finally:
                if compiled is not None:
                    self._release(compiled)

    def test_extension_content_is_never_inferred_from_folder_names(self):
        """Reject a conventional content root that the manifest did not project."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._root_agent(root)
            extension = self._runtime_extension(
                root, "temporal", capabilities=("content.tools",)
            )
            self._write(
                extension / "tools" / "lookup.py",
                "from harnest.agent import tool\n"
                "@tool\n"
                "def lookup(value):\n"
                "    \"\"\"Return the supplied lookup value.\"\"\"\n"
                "    return value\n",
            )
            with (
                patch(
                    "harnest.bundle.get_backend", return_value=self._managed_backend()
                ),
                self.assertRaisesRegex(
                    BundleConventionError, "must declare 'tools' under contributes.tools"
                ),
            ):
                compile_application(
                    root,
                    entrypoint="agent:root_agent",
                    framework="langgraph",
                )

    def test_extension_project_identity_must_match_manifest(self):
        """Keep dependency metadata bound to the discovered extension identity."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._root_agent(root)
            extension = self._runtime_extension(root, "temporal")
            self._write(
                extension / "pyproject.toml",
                "[project]\nname = 'isolated-extension'\nversion = '1.0.0'\n",
            )

            with patch(
                "harnest.bundle.get_backend", return_value=self._managed_backend()
            ):
                with self.assertRaisesRegex(
                    BundleConventionError,
                    "pyproject name 'isolated-extension' must be 'harnest-extension-temporal'",
                ):
                    compile_application(
                        root,
                        entrypoint="agent:root_agent",
                        framework="langgraph",
                    )
            self.assertNotIn("harnest.extensions.temporal", sys.modules)

    def test_extension_project_dependencies_join_the_compiled_descriptor(self):
        """Retain PEP 508 inputs for the shared pre-import environment solve."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._root_agent(root)
            extension = self._runtime_extension(root, "temporal")
            self._write(
                extension / "pyproject.toml",
                "[project]\n"
                "name = 'harnest-extension-temporal'\n"
                "version = '1.2.3'\n"
                "dependencies = ['httpx>=0.28,<1']\n",
            )
            compiled = None
            try:
                with patch(
                    "harnest.bundle.get_backend", return_value=self._managed_backend()
                ):
                    compiled = compile_application(
                        root,
                        entrypoint="agent:root_agent",
                        framework="langgraph",
                    )
                self.assertEqual(
                    compiled.extensions[0].descriptor.dependencies,
                    ("httpx<1,>=0.28",),
                )
            finally:
                if compiled is not None:
                    self._release(compiled)

    def test_extension_imports_root_library_from_the_shared_runtime(self):
        """Activate application helpers before importing same-process extension code."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._root_agent(root)
            extension = self._runtime_extension(root, "temporal")
            self._write(root / "lib" / "shared.py", "SDK_NAME = 'shared-sdk'\n")
            self._write(
                extension / "extension.py",
                "from harnest.lib.shared import SDK_NAME\n"
                "from harnest.extensions import Extension\n"
                "class Temporal(Extension):\n"
                "    sdk_name = SDK_NAME\n"
                "extension = Temporal()\n",
            )
            compiled = None
            try:
                with patch(
                    "harnest.bundle.get_backend",
                    return_value=self._managed_backend(),
                ):
                    compiled = compile_application(
                        root,
                        entrypoint="agent:root_agent",
                        framework="langgraph",
                    )
                self.assertEqual(compiled.extensions[0].extension.sdk_name, "shared-sdk")
            finally:
                if compiled is not None:
                    self._release(compiled)

            self.assertNotIn("harnest.extensions.temporal", sys.modules)

    def test_advanced_mode_rejects_extension_content_but_allows_boundaries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._root_agent(root, advanced=True)
            extension = self._runtime_extension(
                root,
                "temporal",
                capabilities=("content.tools",),
                contributions=("tools",),
            )
            self._write(
                extension / "tools" / "normalize.py",
                "from harnest.agent import tool\n"
                "@tool\n"
                "def normalize(value):\n"
                "    \"\"\"Return the supplied value unchanged.\"\"\"\n"
                "    return value\n",
            )
            with patch(
                "harnest.bundle.get_backend", return_value=self._advanced_backend()
            ):
                with self.assertRaisesRegex(
                    BundleConventionError,
                    "cannot auto-compose extension 'temporal' content: tools",
                ):
                    compile_application(
                        root,
                        entrypoint="agent:root_agent",
                        framework="adk",
                        mode="advanced",
                    )
            self.assertNotIn("harnest.extensions.temporal", sys.modules)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._root_agent(root, advanced=True)
            extension = self._runtime_extension(
                root,
                "temporal",
                capabilities=("lifecycle.agent", "context.resources"),
                contributions=("lifecycle",),
            )
            self._write(
                extension / "lifecycle" / "bindings.py",
                "from harnest import context\n"
                "from harnest import lifecycle\n"
                "@lifecycle.agent.before\n"
                "def observe(scope, value): return scope.next()\n"
                "@context.provider('temporal_client')\n"
                "def temporal_client(): return {'ready': True}\n",
            )
            compiled = None
            try:
                with patch(
                    "harnest.bundle.get_backend",
                    return_value=self._advanced_backend(),
                ):
                    compiled = compile_application(
                        root,
                        entrypoint="agent:root_agent",
                        framework="adk",
                        mode="advanced",
                    )
                self.assertEqual(
                    [item.phase for item in compiled.lifecycle_extensions],
                    ["before_invoke", "context"],
                )
                context_listener = next(
                    item for item in compiled.lifecycle_extensions if item.phase == "context"
                )
                self.assertEqual(context_listener.context_name, "temporal_client")
                self.assertEqual(context_listener.callback(), {"ready": True})
            finally:
                if compiled is not None:
                    self._release(compiled)
            self.assertNotIn("harnest.extensions.temporal", sys.modules)

    def test_artifact_records_extension_provenance_files_and_releases_namespace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "agent"
            output = root / ".harnest" / "compiled"
            self._root_agent(root)
            core = self._runtime_extension(root, "core")
            self._write(core / "lib" / "client.py", "API_LEVEL = 1\n")
            self._runtime_extension(root, "zeta", requires=("core",))

            with patch(
                "harnest.bundle.get_backend", return_value=self._managed_backend()
            ):
                first = compile_artifact(root, output, framework="langgraph")

            descriptors = discover_extensions(root / "extensions")
            self.assertEqual(
                [item["name"] for item in first["extensions"]], ["core", "zeta"]
            )
            self.assertEqual(
                [item["digest"] for item in first["extensions"]],
                [item.digest for item in descriptors],
            )
            file_records = {item["path"]: item for item in first["files"]}
            extension_path = "source/extensions/core/lib/client.py"
            self.assertIn(extension_path, file_records)
            copied = output / extension_path
            self.assertEqual(
                file_records[extension_path]["sha256"],
                hashlib.sha256(copied.read_bytes()).hexdigest(),
            )
            self.assertNotIn("harnest.extensions.core", sys.modules)
            self.assertNotIn("harnest.extensions.zeta", sys.modules)
            self.assertFalse(hasattr(extension_namespace, "core"))
            self.assertFalse(hasattr(extension_namespace, "zeta"))

            self._write(core / "lib" / "client.py", "API_LEVEL = 2\n")
            with patch(
                "harnest.bundle.get_backend", return_value=self._managed_backend()
            ):
                second = compile_artifact(root, output, framework="langgraph")

            self.assertNotEqual(first["digest"], second["digest"])
            self.assertNotEqual(
                first["extensions"][0]["digest"], second["extensions"][0]["digest"]
            )
            self.assertNotIn("harnest.extensions.core", sys.modules)
            self.assertFalse(hasattr(extension_namespace, "core"))

    def test_official_rag_contract_compiles_with_a_dependent_extension(self):
        """Keep RAG imports usable by agents and dependent provider extensions."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "agent"
            output = Path(temp) / "compiled"
            self._root_agent(root)
            shutil.copytree(
                ROOT / "official-extensions" / "rag",
                root / "extensions" / "rag",
            )
            provider = self._runtime_extension(
                root, "search_provider", requires=("rag",)
            )
            self._write(
                provider / "extension.py",
                "from harnest.extensions import Extension\n"
                "from harnest.extensions.rag import RAGBackend, RAGQuery\n"
                "class SearchProviderExtension(Extension):\n"
                "    contract = (RAGBackend, RAGQuery)\n"
                "extension = SearchProviderExtension()\n",
            )
            self._write(
                root / "lifecycle" / "knowledge.py",
                "from harnest import context, lifecycle\n"
                "from harnest.extensions.rag import RAGService, rag\n"
                "knowledge = rag.memory(namespace='compiled-test')\n"
                "@lifecycle.resource\n"
                "@context.provider('knowledge')\n"
                "async def knowledge_resource():\n"
                "    async with knowledge:\n"
                "        yield knowledge\n",
            )
            self._write(
                root / "tools" / "search.py",
                "from harnest import context\n"
                "from harnest.agent import tool\n"
                "from harnest.extensions.rag import RAGService, SearchMode\n"
                "@tool\n"
                "async def search(query: str) -> list[str]:\n"
                "    \"\"\"Search the compiled knowledge resource.\"\"\"\n"
                "    service = context.resource('knowledge', RAGService)\n"
                "    hits = await service.search(query, mode=SearchMode.KEYWORD)\n"
                "    return [hit.chunk.text for hit in hits]\n",
            )

            with patch(
                "harnest.bundle.get_backend", return_value=self._managed_backend()
            ):
                manifest = compile_artifact(root, output, framework="langgraph")

            self.assertEqual(
                [item["name"] for item in manifest["extensions"]],
                ["rag", "search_provider"],
            )
            self.assertIn("asyncpg<1,>=0.30", manifest["extensions"][0]["dependencies"])
            self.assertEqual(manifest["extensions"][1]["requires"], ["rag"])
            self.assertTrue(
                (output / "source" / "extensions" / "rag" / "lib" / "postgres.py").is_file()
            )
            self.assertNotIn("harnest.extensions.rag", sys.modules)
            self.assertNotIn("harnest.extensions.search_provider", sys.modules)


if __name__ == "__main__":
    unittest.main()
