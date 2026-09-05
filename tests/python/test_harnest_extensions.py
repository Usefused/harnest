"""Canonical extension layout, compiler ownership, and upgrade regressions."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harnest.application_layout import ApplicationLayoutError, lifecycle_directory
from harnest.bundle import BundleConventionError, compile_application, compile_artifact
from harnest.extensions import Extension, ExtensionContext, ExtensionImportError
from harnest.plugins import release_runtime_plugins, activate_runtime_plugins
from harnest.runtime_plugins import (
    RUNTIME_PLUGIN_CAPABILITIES,
    discover_application_extensions,
)
from harnest.upgrade import plan_upgrade, apply_upgrade, UpgradeError
from _session_store_fixture import write_session_store


def write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def package(root: Path, name: str = "clock", *, legacy: bool = False, capabilities=()) -> Path:
    directory = root / ("plugins" if legacy else "extensions") / name
    stem = "plugin" if legacy else "extension"
    kind = "RuntimePlugin" if legacy else "Extension"
    base = "Plugin" if legacy else "Extension"
    namespace = "plugins" if legacy else "extensions"
    write(directory / f"{stem}.yaml", f"""apiVersion: harnest.dev/v1alpha1
kind: {kind}
metadata:
  name: {name}
  version: 1.0.0
runtime:
  entrypoint: {stem}:{stem}
capabilities: {list(capabilities)!r}
""")
    write(directory / f"{stem}.py", f"from harnest.{namespace} import {base}\n"
          f"class Clock({base}):\n    pass\n{stem} = Clock()\n")
    return directory


def agent(root: Path, *, legacy: bool = False) -> None:
    write_session_store(root)
    if not legacy:
        (root / "extensions").rename(root / "lifecycle")
    write(root / "agent.py", "from harnest.agent import Agent\nroot_agent = Agent(name='demo', model='test/model')\n")
    write(root / "instructions.md", "Be helpful.\n")
    write(root / "agent-card.yaml", "name: demo\ndescription: Test agent.\n")
    write(root / "config.yaml", "metadata:\n  name: demo\nspec:\n  framework:\n    name: adk\n"
          "  runtime:\n    version: '3.12'\n    dependencyFile: pyproject.toml\n")
    write(root / "pyproject.toml", "[project]\nname = 'demo'\nversion = '0.1.0'\ndependencies = []\n")



class HarnestExtensionTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name) / 'agent'
        self.root.mkdir()

    def test_manifest_schema_accepts_the_canonical_package(self):
        import json
        import jsonschema
        import yaml

        directory = package(self.root)
        schema_path = Path(__file__).resolve().parents[2] / "schemas" / "extension.schema.json"
        schema = json.loads(schema_path.read_text())
        jsonschema.validate(yaml.safe_load((directory / "extension.yaml").read_text()), schema)

    def test_canonical_distribution_name_is_prefixed(self):
        """Separate extension identity from potentially colliding provider SDKs."""

        directory = package(self.root, name="docker")
        write(
            directory / "pyproject.toml",
            "[project]\n"
            "name = 'harnest-extension-docker'\n"
            "version = '1.0.0'\n"
            "dependencies = ['docker>=7.1,<8']\n",
        )

        descriptor = discover_application_extensions(self.root)[0]

        self.assertEqual(descriptor.name, "docker")
        self.assertEqual(descriptor.dependencies, ("docker<8,>=7.1",))

    def test_canonical_distribution_rejects_unprefixed_name(self):
        """Prevent an extension distribution from impersonating its provider SDK."""

        directory = package(self.root, name="docker")
        write(
            directory / "pyproject.toml",
            "[project]\nname = 'docker'\nversion = '1.0.0'\n",
        )

        with self.assertRaisesRegex(
            ValueError, "must be 'harnest-extension-docker'"
        ):
            discover_application_extensions(self.root)

    def test_manifest_schema_uses_runtime_capability_vocabulary(self):
        """Keep extension validation and runtime authority checks synchronized."""
        import json

        schema_path = Path(__file__).resolve().parents[2] / "schemas" / "extension.schema.json"
        schema = json.loads(schema_path.read_text())
        declared = schema["properties"]["capabilities"]["items"]["enum"]

        self.assertEqual(set(declared), set(RUNTIME_PLUGIN_CAPABILITIES))

    def test_incomplete_package_does_not_become_lifecycle_code(self):
        write(self.root / "extensions" / "broken" / "extension.py", "raise AssertionError('must not import')\n")
        with self.assertRaisesRegex(ValueError, "needs extension.yaml"):
            discover_application_extensions(self.root)

    def test_backup_container_symlink_is_rejected(self):
        agent(self.root, legacy=True)
        plan = plan_upgrade(self.root)
        outside = self.root.parent / "outside-backup"
        outside.mkdir()
        (self.root / ".harnest").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(UpgradeError, "symlink"):
            apply_upgrade(plan)
        assert not tuple(outside.iterdir())

    def test_canonical_namespace_and_cleanup(self):
        tmp_path = self.root
        package(tmp_path)
        descriptors = discover_application_extensions(tmp_path)
        activated = activate_runtime_plugins(descriptors)
        try:
            assert isinstance(activated[0].plugin, Extension)
            assert sys.modules["harnest.extensions.clock"] is sys.modules["harnest.plugins.clock"]
            assert activated[0].module.extension is activated[0].plugin
            assert ExtensionContext("clock").extension_name == "clock"
        finally:
            release_runtime_plugins(descriptors)
        assert "harnest.extensions.clock" not in sys.modules
        assert "harnest.plugins.clock" not in sys.modules


    def test_compile_canonical_hooks_checks_manifest_authority(self):
        tmp_path = self.root
        agent(tmp_path)
        directory = package(tmp_path)
        write(directory / "lifecycle" / "audit.py", "from harnest.lifecycle import lifecycle\n"
              "@lifecycle.tool.before\ndef audit(context, call):\n    return call\n")
        backend = SimpleNamespace(lower_managed=lambda value, **kwargs: value,
                                  wrap_managed=lambda *args, **kwargs: None)
        with patch("harnest.bundle.get_backend", return_value=backend):
            with self.assertRaisesRegex(BundleConventionError, "lifecycle.tool"):
                compile_application(tmp_path, entrypoint="agent:root_agent")
            package(tmp_path, capabilities=("lifecycle.tool",))
            application = compile_application(tmp_path, entrypoint="agent:root_agent")
        try:
            assert any(item.relative_path == "extensions/clock/lifecycle/audit.py" for item in application.extensions)
        finally:
            release_runtime_plugins(discover_application_extensions(tmp_path))

    def test_compile_preserves_regular_extension_readme(self):
        """Keep package-facing documentation inside the compiled source tree."""

        agent(self.root)
        directory = package(self.root)
        write(directory / "README.md", "# Clock extension\n")
        backend = SimpleNamespace(
            lower_managed=lambda value, **kwargs: value,
            wrap_managed=lambda *args, **kwargs: None,
        )
        with patch("harnest.bundle.get_backend", return_value=backend):
            manifest = compile_artifact(
                self.root,
                self.root.parent / "artifact",
                entrypoint="agent:root_agent",
            )
        paths = {item["path"] for item in manifest["files"]}
        self.assertIn("source/extensions/clock/README.md", paths)
        self.assertEqual(
            (
                self.root.parent
                / "artifact/source/extensions/clock/README.md"
            ).read_text("utf-8"),
            "# Clock extension\n",
        )

    def test_extension_layout_allows_only_a_regular_root_readme(self):
        """Keep the README exception narrow within the closed extension root."""

        for kind in ("directory", "symlink", "special", "unexpected"):
            with self.subTest(kind=kind):
                root = self.root / kind
                root.mkdir()
                agent(root)
                directory = package(root)
                readme = directory / "README.md"
                if kind == "directory":
                    readme.mkdir()
                elif kind == "symlink":
                    readme.symlink_to(self.root / "outside.md")
                elif kind == "special":
                    os.mkfifo(readme)
                else:
                    write(directory / "NOTES.md", "unexpected\n")
                backend = SimpleNamespace(
                    lower_managed=lambda value, **kwargs: value,
                    wrap_managed=lambda *args, **kwargs: None,
                )
                with (
                    patch("harnest.bundle.get_backend", return_value=backend),
                    self.assertRaisesRegex(BundleConventionError, "resource"),
                ):
                    compile_application(root, entrypoint="agent:root_agent")


    def test_mixed_layout_rejected_before_import(self):
        tmp_path = self.root
        (tmp_path / "lifecycle").mkdir()
        write(tmp_path / "extensions" / "storage.py", "raise AssertionError('must not run')")
        with self.assertRaisesRegex(ApplicationLayoutError, "Run `harnest upgrade"):
            lifecycle_directory(tmp_path)


    def test_upgrade_moves_lifecycle_before_reusing_extensions(self):
        tmp_path = self.root
        agent(tmp_path, legacy=True)
        package(tmp_path, legacy=True)
        original = (tmp_path / "extensions" / "sessions.py").read_bytes()
        plan = plan_upgrade(tmp_path)
        assert not plan.blockers
        backup = apply_upgrade(plan)
        assert (tmp_path / "lifecycle" / "sessions.py").read_bytes() == original
        assert (backup / "extensions" / "sessions.py").read_bytes() == original
        assert (tmp_path / "extensions" / "clock" / "extension.py").is_file()
        descriptors = discover_application_extensions(tmp_path)
        activated = activate_runtime_plugins(descriptors)
        try:
            assert activated[0].module.extension is activated[0].module.plugin
        finally:
            release_runtime_plugins(descriptors)
        assert not plan_upgrade(tmp_path).actions


    def test_upgrade_refuses_destination_created_after_preview(self):
        tmp_path = self.root
        agent(tmp_path, legacy=True)
        plan = plan_upgrade(tmp_path)
        (tmp_path / "lifecycle").mkdir()
        with self.assertRaisesRegex(UpgradeError, "destination changed"):
            apply_upgrade(plan)
        assert not (tmp_path / ".harnest" / "upgrade-backups").exists()


    def test_duplicate_legacy_and_canonical_package_names_fail(self):
        tmp_path = self.root
        package(tmp_path, legacy=True)
        package(tmp_path)
        with self.assertRaisesRegex(ValueError, "collision"):
            discover_application_extensions(tmp_path)

    def test_dependencies_span_legacy_and_canonical_packages(self):
        package(self.root, "legacy", legacy=True)
        directory = package(self.root, "canonical")
        manifest = directory / "extension.yaml"
        write(manifest, manifest.read_text() + "requires:\n  extensions: [legacy]\n")
        descriptors = discover_application_extensions(self.root)
        self.assertEqual([item.name for item in descriptors], ["legacy", "canonical"])

    def test_canonical_manifest_symlink_is_rejected_without_import(self):
        directory = package(self.root)
        manifest = directory / "extension.yaml"
        outside = self.root.parent / "manifest.yaml"
        manifest.rename(outside)
        manifest.symlink_to(outside)
        write(directory / "extension.py", "raise AssertionError('must not import')\n")
        with self.assertRaisesRegex(ValueError, "symlink"):
            discover_application_extensions(self.root)


    def test_upgrade_package_collisions_are_non_mutating_extension_py(self):
        tmp_path = self.root
        collision = "extension.py"
        agent(tmp_path, legacy=True)
        directory = package(tmp_path, legacy=True)
        write(directory / "extensions" / "audit.py", "# lifecycle\n")
        write(directory / collision, "preserve this authored content\n")
        before = (directory / "plugin.py").read_bytes()
        plan = plan_upgrade(tmp_path)
        assert plan.blockers
        with self.assertRaisesRegex(UpgradeError, "blockers"):
            apply_upgrade(plan)
        assert (directory / "plugin.py").read_bytes() == before
        assert not (tmp_path / ".harnest").exists()


    def test_upgrade_package_collisions_are_non_mutating_extension_yaml(self):
        tmp_path = self.root
        collision = "extension.yaml"
        agent(tmp_path, legacy=True)
        directory = package(tmp_path, legacy=True)
        write(directory / "extensions" / "audit.py", "# lifecycle\n")
        write(directory / collision, "preserve this authored content\n")
        before = (directory / "plugin.py").read_bytes()
        plan = plan_upgrade(tmp_path)
        assert plan.blockers
        with self.assertRaisesRegex(UpgradeError, "blockers"):
            apply_upgrade(plan)
        assert (directory / "plugin.py").read_bytes() == before
        assert not (tmp_path / ".harnest").exists()


    def test_upgrade_package_collisions_are_non_mutating_lifecycle(self):
        tmp_path = self.root
        collision = "lifecycle"
        agent(tmp_path, legacy=True)
        directory = package(tmp_path, legacy=True)
        write(directory / "extensions" / "audit.py", "# lifecycle\n")
        write(directory / collision, "preserve this authored content\n")
        before = (directory / "plugin.py").read_bytes()
        plan = plan_upgrade(tmp_path)
        assert plan.blockers
        with self.assertRaisesRegex(UpgradeError, "blockers"):
            apply_upgrade(plan)
        assert (directory / "plugin.py").read_bytes() == before
        assert not (tmp_path / ".harnest").exists()


    def test_upgrade_preserves_existing_extension_binding(self):
        tmp_path = self.root
        agent(tmp_path, legacy=True)
        directory = package(tmp_path, legacy=True)
        source = directory / "plugin.py"
        write(source, source.read_text() + "\ndef extension():\n    return 'business data'\n")
        assert any("already binds" in item for item in plan_upgrade(tmp_path).blockers)


    def test_canonical_import_failure_rolls_back_both_namespaces(self):
        tmp_path = self.root
        directory = package(tmp_path)
        write(directory / "extension.py", "raise RuntimeError('private-provider-detail')\n")
        descriptors = discover_application_extensions(tmp_path)
        with self.assertRaises(ExtensionImportError) as failure:
            activate_runtime_plugins(descriptors)
        assert "private-provider-detail" not in str(failure.exception)
        assert "harnest.extensions.clock" not in sys.modules
        assert "harnest.plugins.clock" not in sys.modules


    def test_upgrade_recognizes_extension_owned_storage(self):
        tmp_path = self.root
        agent(tmp_path)
        directory = package(tmp_path, capabilities=("storage.sessions", "storage.checkpoints"))
        (tmp_path / "lifecycle").rename(directory / "lifecycle")
        assert not any(item.path.endswith("storage.py") for item in plan_upgrade(tmp_path).actions)


    def test_upgrade_rejects_parent_symlink_swap(self):
        tmp_path = self.root
        agent(tmp_path)
        # A planned create must not follow a new parent link outside the project.
        from harnest.upgrade import UpgradeAction, UpgradePlan
        outside = tmp_path.parent / (tmp_path.name + "-outside")
        outside.mkdir()
        plan = UpgradePlan(tmp_path, "adk", (UpgradeAction("create", "lib/example.py", "test", content="pass\n"),), ())
        (tmp_path / "lib").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(UpgradeError, "symlink"):
            apply_upgrade(plan)
        assert not (outside / "example.py").exists()
