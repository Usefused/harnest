import os
from pathlib import Path
import tempfile
import unittest

from harnest.plugin import discover_plugins
from harnest.runtime_plugins import (
    RUNTIME_PLUGIN_CAPABILITIES,
    RuntimePluginConventionError,
    discover_runtime_plugins,
    runtime_plugin_digest,
    verify_runtime_plugin,
)


class RuntimePluginDiscoveryTests(unittest.TestCase):
    def _write(self, path: Path, contents: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    def _manifest(
        self,
        name: str,
        *,
        version: str = "1.2.3",
        requires: tuple[str, ...] = (),
        capabilities: tuple[str, ...] = (),
    ) -> str:
        lines = [
            "apiVersion: harnest.dev/v1alpha1",
            "kind: RuntimePlugin",
            "metadata:",
            f"  name: {name}",
            f"  version: {version}",
            "runtime:",
            "  entrypoint: plugin:plugin",
        ]
        if requires:
            lines.extend(("requires:", "  plugins:"))
            lines.extend(f"    - {required}" for required in requires)
        if capabilities:
            lines.append("capabilities:")
            lines.extend(f"  - {capability}" for capability in capabilities)
        return "\n".join(lines) + "\n"

    def _runtime_plugin(
        self,
        root: Path,
        name: str,
        *,
        version: str = "1.2.3",
        requires: tuple[str, ...] = (),
        capabilities: tuple[str, ...] = (),
        source: str = "from harnest.plugins import Plugin\nclass Example(Plugin): pass\nplugin = Example()\n",
    ) -> Path:
        directory = root / name
        self._write(
            directory / "plugin.yaml",
            self._manifest(
                name,
                version=version,
                requires=requires,
                capabilities=capabilities,
            ),
        )
        self._write(directory / "plugin.py", source)
        return directory

    def test_discovers_dependency_order_with_lexical_tie_break(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugins"
            self._runtime_plugin(root, "zeta", requires=("core",))
            self._runtime_plugin(root, "core", version="2.0.0-rc.1+build.7")
            self._runtime_plugin(
                root,
                "audit",
                capabilities=("storage.custom", "lifecycle.agent"),
            )

            descriptors = discover_runtime_plugins(root)

        self.assertEqual([item.name for item in descriptors], ["audit", "core", "zeta"])
        self.assertEqual(descriptors[1].version, "2.0.0-rc.1+build.7")
        self.assertEqual(descriptors[2].requires, ("core",))
        self.assertEqual(
            descriptors[0].capabilities,
            ("lifecycle.agent", "storage.custom"),
        )
        self.assertTrue(descriptors[0].digest.startswith("sha256:"))
        self.assertEqual(descriptors[0].namespace, "harnest.plugins.audit")
        self.assertEqual(descriptors[0].source.name, "plugin.py")

    def test_digest_excludes_files_omitted_from_compiled_source(self):
        """Keep authored and artifact plugin identity stable across local state."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugins"
            directory = self._runtime_plugin(root, "temporal")
            before = runtime_plugin_digest(directory)
            self._write(directory / ".env", "TOKEN=private\n")
            self._write(directory / ".git" / "state", "local\n")
            self._write(directory / "__pycache__" / "plugin.pyc", "cache\n")

            after = runtime_plugin_digest(directory)

        self.assertEqual(after, before)

    def test_manifestless_folders_retain_agent_plugin_behavior(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugins"
            self._write(root / "_README.md", "Plugin folders live here.\n")
            self._write(root / "_notes" / "README.md", "Ignored placeholder.\n")
            self._write(root / "support" / "mcp" / "support.py", "client = None\n")
            self._write(
                root / "support" / "skills" / "triage" / "SKILL.md",
                "name: triage\n",
            )
            self._runtime_plugin(root, "temporal")

            runtime = discover_runtime_plugins(root)
            agent_plugins = discover_plugins(root)

        self.assertEqual([item.name for item in runtime], ["temporal"])
        self.assertEqual([item.name for item in agent_plugins], ["support"])

    def test_rejects_ambiguous_or_unknown_manifest_shapes(self):
        cases = {
            "duplicate": (
                "apiVersion: harnest.dev/v1alpha1\napiVersion: harnest.dev/v1alpha1\n",
                "duplicate",
            ),
            "multiple": (
                self._manifest("multiple") + "---\n{}\n",
                "exactly one YAML document",
            ),
            "unknown": (
                self._manifest("unknown") + "authority: all\n",
                "unknown runtime plugin manifest fields",
            ),
            "wrongcaps": (
                self._manifest("wrongcaps") + "capabilities: lifecycle.agent\n",
                "capabilities must be a list",
            ),
            "unknowncap": (
                self._manifest("unknowncap") + "capabilities: [lifecycle.everything]\n",
                "unknown runtime plugin capabilities",
            ),
            "numeric": (
                self._manifest("numeric", version="1.2"),
                "metadata.version must be a non-empty string",
            ),
            "badsemver": (
                self._manifest("badsemver", version="1.2.x"),
                "valid semantic version",
            ),
            "leadingzero": (
                self._manifest("leadingzero", version="01.2.3"),
                "valid semantic version",
            ),
            "numericprereleasezero": (
                self._manifest("numericprereleasezero", version="1.2.3-01"),
                "valid semantic version",
            ),
            "entry": (
                self._manifest("entry").replace("plugin:plugin", "main:plugin"),
                "entrypoint must be 'plugin:plugin'",
            ),
            "dependency": (
                self._manifest("dependency") + "requires:\n  plugins: core\n",
                "requires.plugins must be a list",
            ),
        }
        for folder, (manifest, expected) in cases.items():
            with (
                self.subTest(folder=folder),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary) / "plugins"
                self._write(root / folder / "plugin.yaml", manifest)
                self._write(root / folder / "plugin.py", "plugin = object()\n")
                with self.assertRaisesRegex(RuntimePluginConventionError, expected):
                    discover_runtime_plugins(root)

    def test_rejects_name_mismatch_keywords_and_duplicate_declarations(self):
        cases = {
            "mismatch": (
                self._manifest("different"),
                "must match folder",
            ),
            "class": (
                self._manifest("class"),
                "non-keyword Python identifier",
            ),
            "capdup": (
                self._manifest("capdup")
                + "capabilities: [lifecycle.agent, lifecycle.agent]\n",
                "duplicate runtime plugin capabilities",
            ),
            "depdup": (
                self._manifest("depdup") + "requires:\n  plugins: [other, other]\n",
                "duplicate runtime plugin dependencies",
            ),
        }
        for folder, (manifest, expected) in cases.items():
            with (
                self.subTest(folder=folder),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary) / "plugins"
                self._write(root / folder / "plugin.yaml", manifest)
                self._write(root / folder / "plugin.py", "plugin = object()\n")
                with self.assertRaisesRegex(RuntimePluginConventionError, expected):
                    discover_runtime_plugins(root)

    def test_rejects_missing_self_and_cyclic_dependencies(self):
        cases = {
            "missing": (("alpha", ("absent",)),),
            "self": (("alpha", ("alpha",)),),
            "cycle": (("alpha", ("beta",)), ("beta", ("alpha",))),
        }
        expected = {
            "missing": "requires missing plugins",
            "self": "cannot require itself",
            "cycle": "dependency cycle",
        }
        for case, declarations in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "plugins"
                for name, requires in declarations:
                    self._runtime_plugin(root, name, requires=requires)
                with self.assertRaisesRegex(
                    RuntimePluginConventionError, expected[case]
                ):
                    discover_runtime_plugins(root)

    def test_rejects_casefold_collisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugins"
            self._runtime_plugin(root, "Alpha")
            self._runtime_plugin(root, "alpha")
            if len(tuple(root.iterdir())) < 2:
                self.skipTest("filesystem is case-insensitive")
            with self.assertRaisesRegex(RuntimePluginConventionError, "name collision"):
                discover_runtime_plugins(root)

    @unittest.skipIf(os.name == "nt", "symlink policy requires POSIX test support")
    def test_rejects_symlinked_manifest_entrypoint_and_nested_content(self):
        targets = ("plugin.yaml", "plugin.py", "helpers/linked.py")
        for target in targets:
            with (
                self.subTest(target=target),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary) / "plugins"
                directory = self._runtime_plugin(root, "unsafe")
                destination = directory / target
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    destination.unlink()
                destination.symlink_to(directory / "plugin.py")
                with self.assertRaisesRegex(RuntimePluginConventionError, "symlink"):
                    discover_runtime_plugins(root)

    def test_digest_detects_post_discovery_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugins"
            directory = self._runtime_plugin(root, "mutable")
            self._write(directory / "helpers.py", "VALUE = 1\n")
            descriptor = discover_runtime_plugins(root)[0]
            self.assertEqual(descriptor.digest, runtime_plugin_digest(directory))

            self._write(directory / "helpers.py", "VALUE = 2\n")

            with self.assertRaisesRegex(RuntimePluginConventionError, "changed"):
                verify_runtime_plugin(descriptor)

    def test_exported_capability_vocabulary_is_closed_and_dotted(self):
        self.assertIn("lifecycle.agent", RUNTIME_PLUGIN_CAPABILITIES)
        self.assertIn("context.credentials", RUNTIME_PLUGIN_CAPABILITIES)
        self.assertIn("context.continuations", RUNTIME_PLUGIN_CAPABILITIES)
        self.assertIn("context.skills", RUNTIME_PLUGIN_CAPABILITIES)
        self.assertIn("lifecycle.skills", RUNTIME_PLUGIN_CAPABILITIES)
        self.assertIn("native.langgraph", RUNTIME_PLUGIN_CAPABILITIES)
        self.assertIn("sandbox.provider", RUNTIME_PLUGIN_CAPABILITIES)
        self.assertTrue(all("." in value for value in RUNTIME_PLUGIN_CAPABILITIES))


if __name__ == "__main__":
    unittest.main()
