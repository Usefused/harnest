import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import harnest.plugins as plugin_namespace
from harnest.bundle import (
    BundleConventionError,
    BundleDuplicateError,
    _artifact_digest,
    compile_application,
    compile_artifact,
)
from harnest.plugins import release_runtime_plugins
from harnest.runtime_plugins import discover_runtime_plugins

from _session_store_fixture import write_session_store
from _skill_fixture import run_skill_tool


class RuntimePluginCompilerIntegrationTests(unittest.TestCase):
    def test_plugin_provenance_digest_matches_engine_contract(self):
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
        )

        self.assertEqual(
            digest,
            "sha256:60f624710da7d43330475fcd21951e7"
            "4659f7107da9fa71b21a83a84ffd41b15",
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

    def _runtime_plugin(
        self,
        root: Path,
        name: str,
        *,
        capabilities: tuple[str, ...] = (),
        requires: tuple[str, ...] = (),
    ) -> Path:
        """Author one strict descriptor and its public plugin singleton."""

        plugin = root / "plugins" / name
        requires_yaml = ""
        if requires:
            rendered = "\n".join(f"    - {item}" for item in requires)
            requires_yaml = f"requires:\n  plugins:\n{rendered}\n"
        capabilities_yaml = ""
        if capabilities:
            rendered = "\n".join(f"  - {item}" for item in capabilities)
            capabilities_yaml = f"capabilities:\n{rendered}\n"
        self._write(
            plugin / "plugin.yaml",
            "apiVersion: harnest.dev/v1alpha1\n"
            "kind: RuntimePlugin\n"
            f"metadata:\n  name: {name}\n  version: 1.2.3\n"
            "runtime:\n  entrypoint: plugin:plugin\n"
            f"{requires_yaml}{capabilities_yaml}",
        )
        self._write(
            plugin / "plugin.py",
            "from harnest.plugins import Plugin\n"
            "class AuthoredPlugin(Plugin):\n"
            "    pass\n"
            "plugin = AuthoredPlugin()\n",
        )
        return plugin

    def _full_plugin_content(self, plugin: Path) -> None:
        """Add local tool, MCP, skill, and lifecycle contributions to a plugin."""

        self._write(
            plugin / "tools" / "normalize.py",
            "from harnest.tool import tool\n"
            "@tool\n"
            "def normalize(value: str) -> str:\n"
            "    \"\"\"Normalize a catalog lookup value.\"\"\"\n"
            "    return value.strip().lower()\n",
        )
        self._write(
            plugin / "mcp" / "catalog.py",
            "from harnest.mcp import MCPClient\n"
            "def client():\n"
            "    return MCPClient.streamable_http(\n"
            "        'https://offline.invalid/mcp', prefix='catalog'\n"
            "    )\n",
        )
        self._write(
            plugin / "skills" / "catalog-guide" / "SKILL.md",
            "---\n"
            "name: catalog-guide\n"
            "description: Explain how to use the local catalog capability.\n"
            "---\n\n"
            "# Catalog guide\n\nUse the catalog only when requested.\n",
        )
        self._write(
            plugin / "extensions" / "audit.py",
            "from harnest.lifecycle import lifecycle\n"
            "@lifecycle.agent.before(order=4)\n"
            "def audit(context, value):\n"
            "    return context.next()\n",
        )

    @staticmethod
    def _release(compiled) -> None:
        """Release direct-compile acquisitions whose ownership did not reach a runtime."""

        release_runtime_plugins(
            tuple(item.descriptor for item in compiled.plugins)
        )

    def test_managed_compile_composes_runtime_plugin_for_both_frameworks(self):
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
                plugin = self._runtime_plugin(
                    root, "temporal", capabilities=capabilities
                )
                self._full_plugin_content(plugin)
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
                        "plugin__temporal__mcp__catalog",
                    )
                    self.assertEqual(
                        [item.descriptor.name for item in compiled.plugins],
                        ["temporal"],
                    )
                    plugin_listener = next(
                        item
                        for item in compiled.extensions
                        if item.relative_path
                        == "plugins/temporal/extensions/audit.py"
                    )
                    self.assertEqual(plugin_listener.phase, "before_invoke")
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
                self.assertNotIn("harnest.plugins.temporal", sys.modules)
                self.assertFalse(hasattr(plugin_namespace, "temporal"))

    def test_manifest_capabilities_gate_content_and_extensions(self):
        cases = (
            ("tools", "content.tools", "tools"),
            ("extensions", "lifecycle.agent", "extensions/audit.py"),
            ("skill_source", "lifecycle.skills", "extensions/skills.py"),
        )
        for contribution, capability, source in cases:
            with self.subTest(contribution=contribution), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self._root_agent(root)
                plugin = self._runtime_plugin(root, "temporal")
                if contribution == "tools":
                    self._write(
                        plugin / "tools" / "normalize.py",
                        "from harnest.tool import tool\n"
                        "@tool\n"
                        "def normalize(value):\n"
                        "    \"\"\"Return the supplied value unchanged.\"\"\"\n"
                        "    return value\n",
                    )
                elif contribution == "extensions":
                    self._write(
                        plugin / "extensions" / "audit.py",
                        "from harnest.lifecycle import lifecycle\n"
                        "@lifecycle.agent.before\n"
                        "def audit(context, value): return context.next()\n",
                    )
                else:
                    self._write(
                        plugin / "extensions" / "skills.py",
                        "from harnest.lifecycle import lifecycle\n"
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
                self.assertNotIn("harnest.plugins.temporal", sys.modules)
                self.assertFalse(hasattr(plugin_namespace, "temporal"), source)

    def test_root_and_runtime_plugin_tool_name_collision_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._root_agent(root)
            source = (
                "from harnest.tool import tool\n"
                "@tool\n"
                "def normalize(value):\n"
                "    \"\"\"Return the supplied value unchanged.\"\"\"\n"
                "    return value\n"
            )
            self._write(root / "tools" / "normalize.py", source)
            plugin = self._runtime_plugin(
                root, "temporal", capabilities=("content.tools",)
            )
            self._write(plugin / "tools" / "normalize.py", source)

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
            self.assertNotIn("harnest.plugins.temporal", sys.modules)

    def test_plugin_project_identity_must_match_manifest(self):
        """Keep dependency metadata bound to the discovered plugin identity."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._root_agent(root)
            plugin = self._runtime_plugin(root, "temporal")
            self._write(
                plugin / "pyproject.toml",
                "[project]\nname = 'isolated-plugin'\nversion = '1.0.0'\n",
            )

            with patch(
                "harnest.bundle.get_backend", return_value=self._managed_backend()
            ):
                with self.assertRaisesRegex(
                    BundleConventionError, "pyproject name 'isolated-plugin' must match"
                ):
                    compile_application(
                        root,
                        entrypoint="agent:root_agent",
                        framework="langgraph",
                    )
            self.assertNotIn("harnest.plugins.temporal", sys.modules)

    def test_plugin_project_dependencies_join_the_compiled_descriptor(self):
        """Retain PEP 508 inputs for the shared pre-import environment solve."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._root_agent(root)
            plugin = self._runtime_plugin(root, "temporal")
            self._write(
                plugin / "pyproject.toml",
                "[project]\n"
                "name = 'temporal'\n"
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
                    compiled.plugins[0].descriptor.dependencies,
                    ("httpx<1,>=0.28",),
                )
            finally:
                if compiled is not None:
                    self._release(compiled)

    def test_plugin_imports_root_library_from_the_shared_runtime(self):
        """Activate application helpers before importing same-process plugin code."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._root_agent(root)
            plugin = self._runtime_plugin(root, "temporal")
            self._write(root / "lib" / "shared.py", "SDK_NAME = 'shared-sdk'\n")
            self._write(
                plugin / "plugin.py",
                "from harnest.lib.shared import SDK_NAME\n"
                "from harnest.plugins import Plugin\n"
                "class Temporal(Plugin):\n"
                "    sdk_name = SDK_NAME\n"
                "plugin = Temporal()\n",
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
                self.assertEqual(compiled.plugins[0].plugin.sdk_name, "shared-sdk")
            finally:
                if compiled is not None:
                    self._release(compiled)

            self.assertNotIn("harnest.plugins.temporal", sys.modules)

    def test_advanced_mode_rejects_plugin_content_but_allows_boundaries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._root_agent(root, advanced=True)
            plugin = self._runtime_plugin(
                root, "temporal", capabilities=("content.tools",)
            )
            self._write(
                plugin / "tools" / "normalize.py",
                "from harnest.tool import tool\n"
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
                    "cannot auto-compose runtime plugin 'temporal' content: tools",
                ):
                    compile_application(
                        root,
                        entrypoint="agent:root_agent",
                        framework="adk",
                        mode="advanced",
                    )
            self.assertNotIn("harnest.plugins.temporal", sys.modules)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._root_agent(root, advanced=True)
            plugin = self._runtime_plugin(
                root,
                "temporal",
                capabilities=("lifecycle.agent", "context.resources"),
            )
            self._write(
                plugin / "extensions" / "bindings.py",
                "from harnest.context import context\n"
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.agent.before\n"
                "def observe(scope, value): return scope.next()\n"
                "@context('temporal_client')\n"
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
                    [item.phase for item in compiled.extensions],
                    ["before_invoke", "context"],
                )
                context_listener = next(
                    item for item in compiled.extensions if item.phase == "context"
                )
                self.assertEqual(context_listener.context_name, "temporal_client")
                self.assertEqual(context_listener.callback(), {"ready": True})
            finally:
                if compiled is not None:
                    self._release(compiled)
            self.assertNotIn("harnest.plugins.temporal", sys.modules)

    def test_artifact_records_plugin_provenance_files_and_releases_namespace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "agent"
            output = root / ".harnest" / "compiled"
            self._root_agent(root)
            core = self._runtime_plugin(root, "core")
            self._write(core / "lib" / "client.py", "API_LEVEL = 1\n")
            self._runtime_plugin(root, "zeta", requires=("core",))

            with patch(
                "harnest.bundle.get_backend", return_value=self._managed_backend()
            ):
                first = compile_artifact(root, output, framework="langgraph")

            descriptors = discover_runtime_plugins(root / "plugins")
            self.assertEqual(
                [item["name"] for item in first["plugins"]], ["core", "zeta"]
            )
            self.assertEqual(
                [item["digest"] for item in first["plugins"]],
                [item.digest for item in descriptors],
            )
            file_records = {item["path"]: item for item in first["files"]}
            plugin_path = "source/plugins/core/lib/client.py"
            self.assertIn(plugin_path, file_records)
            copied = output / plugin_path
            self.assertEqual(
                file_records[plugin_path]["sha256"],
                hashlib.sha256(copied.read_bytes()).hexdigest(),
            )
            self.assertNotIn("harnest.plugins.core", sys.modules)
            self.assertNotIn("harnest.plugins.zeta", sys.modules)
            self.assertFalse(hasattr(plugin_namespace, "core"))
            self.assertFalse(hasattr(plugin_namespace, "zeta"))

            self._write(core / "lib" / "client.py", "API_LEVEL = 2\n")
            with patch(
                "harnest.bundle.get_backend", return_value=self._managed_backend()
            ):
                second = compile_artifact(root, output, framework="langgraph")

            self.assertNotEqual(first["digest"], second["digest"])
            self.assertNotEqual(
                first["plugins"][0]["digest"], second["plugins"][0]["digest"]
            )
            self.assertNotIn("harnest.plugins.core", sys.modules)
            self.assertFalse(hasattr(plugin_namespace, "core"))


if __name__ == "__main__":
    unittest.main()
