import os
from pathlib import Path
import tempfile
import unittest

from harnest.extension_descriptors import (
    EXTENSION_CAPABILITIES,
    ExtensionConventionError,
    discover_extensions,
    extension_digest,
    verify_extension,
)


class HarnestExtensionDiscoveryTests(unittest.TestCase):
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
        contributions: tuple[str, ...] = (),
    ) -> str:
        lines = [
            "apiVersion: harnest.dev/v1alpha1",
            "kind: Extension",
            "metadata:",
            f"  name: {name}",
            f"  version: {version}",
            "runtime:",
            "  entrypoint: extension:extension",
        ]
        if requires:
            lines.extend(("requires:", "  extensions:"))
            lines.extend(f"    - {required}" for required in requires)
        if contributions:
            lines.append("contributes:")
            lines.extend(f"  {kind}: [{kind}/]" for kind in contributions)
        if capabilities:
            lines.append("capabilities:")
            lines.extend(f"  - {capability}" for capability in capabilities)
        return "\n".join(lines) + "\n"

    def _runtime_extension(
        self,
        root: Path,
        name: str,
        *,
        version: str = "1.2.3",
        requires: tuple[str, ...] = (),
        capabilities: tuple[str, ...] = (),
        contributions: tuple[str, ...] = (),
        source: str = "from harnest.extensions import Extension\nclass Example(Extension): pass\nextension = Example()\n",
    ) -> Path:
        directory = root / name
        self._write(
            directory / "extension.yaml",
            self._manifest(
                name,
                version=version,
                requires=requires,
                capabilities=capabilities,
                contributions=contributions,
            ),
        )
        self._write(directory / "extension.py", source)
        for contribution in contributions:
            (directory / contribution).mkdir(parents=True, exist_ok=True)
        return directory

    def test_discovers_dependency_order_with_lexical_tie_break(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "extensions"
            self._runtime_extension(root, "zeta", requires=("core",))
            self._runtime_extension(root, "core", version="2.0.0-rc.1+build.7")
            self._runtime_extension(
                root,
                "audit",
                capabilities=("storage.custom", "lifecycle.agent"),
                contributions=("lifecycle",),
            )

            descriptors = discover_extensions(root)

        self.assertEqual([item.name for item in descriptors], ["audit", "core", "zeta"])
        self.assertEqual(descriptors[1].version, "2.0.0-rc.1+build.7")
        self.assertEqual(descriptors[2].requires, ("core",))
        self.assertEqual(
            descriptors[0].capabilities,
            ("lifecycle.agent", "storage.custom"),
        )
        self.assertTrue(descriptors[0].digest.startswith("sha256:"))
        self.assertEqual(descriptors[0].namespace, "harnest.extensions.audit")
        self.assertEqual(descriptors[0].source.name, "extension.py")
        self.assertEqual(descriptors[0].contributions, (("lifecycle", ("lifecycle",)),))

    def test_contributions_are_explicit_validated_and_capability_gated(self):
        cases = {
            "missing": ("contributes:\n  tools: [missing/]\n", "existing regular directory"),
            "escape": ("contributes:\n  tools: [../tools/]\n", "safe package-relative"),
            "reserved": ("contributes:\n  tools: [extension.py/]\n", "safe package-relative"),
            "overlap": (
                "contributes:\n  tools: [content/]\n  skills: [content/skills/]\n",
                "contribution paths overlap",
            ),
        }
        for name, (declaration, expected) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "extensions"
                directory = root / name
                self._write(
                    directory / "extension.yaml",
                    self._manifest(name, capabilities=("content.tools", "content.skills"))
                    + declaration,
                )
                self._write(directory / "extension.py", "extension = object()\n")
                (directory / "content" / "skills").mkdir(parents=True, exist_ok=True)
                with self.assertRaisesRegex(ExtensionConventionError, expected):
                    discover_extensions(root)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "extensions"
            self._runtime_extension(root, "tools", contributions=("tools",))
            with self.assertRaisesRegex(
                ExtensionConventionError, "must declare capability 'content.tools'"
            ):
                discover_extensions(root)

    def test_digest_excludes_files_omitted_from_compiled_source(self):
        """Keep authored and artifact extension identity stable across local state."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "extensions"
            directory = self._runtime_extension(root, "temporal")
            before = extension_digest(directory)
            self._write(directory / ".env", "TOKEN=private\n")
            self._write(directory / ".git" / "state", "local\n")
            self._write(directory / "__pycache__" / "extension.pyc", "cache\n")

            after = extension_digest(directory)

        self.assertEqual(after, before)

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
                "unknown Harnest Extension manifest fields",
            ),
            "wrongcaps": (
                self._manifest("wrongcaps") + "capabilities: lifecycle.agent\n",
                "capabilities must be a list",
            ),
            "unknowncap": (
                self._manifest("unknowncap") + "capabilities: [lifecycle.everything]\n",
                "unknown Harnest Extension capabilities",
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
                self._manifest("entry").replace("extension:extension", "main:extension"),
                "entrypoint must be 'extension:extension'",
            ),
            "dependency": (
                self._manifest("dependency") + "requires:\n  extensions: core\n",
                "requires.extensions must be a list",
            ),
        }
        for folder, (manifest, expected) in cases.items():
            with (
                self.subTest(folder=folder),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary) / "extensions"
                self._write(root / folder / "extension.yaml", manifest)
                self._write(root / folder / "extension.py", "extension = object()\n")
                with self.assertRaisesRegex(ExtensionConventionError, expected):
                    discover_extensions(root)

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
                "duplicate Harnest Extension capabilities",
            ),
            "depdup": (
                self._manifest("depdup") + "requires:\n  extensions: [other, other]\n",
                "duplicate Harnest Extension dependencies",
            ),
        }
        for folder, (manifest, expected) in cases.items():
            with (
                self.subTest(folder=folder),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary) / "extensions"
                self._write(root / folder / "extension.yaml", manifest)
                self._write(root / folder / "extension.py", "extension = object()\n")
                with self.assertRaisesRegex(ExtensionConventionError, expected):
                    discover_extensions(root)

    def test_rejects_missing_self_and_cyclic_dependencies(self):
        cases = {
            "missing": (("alpha", ("absent",)),),
            "self": (("alpha", ("alpha",)),),
            "cycle": (("alpha", ("beta",)), ("beta", ("alpha",))),
        }
        expected = {
            "missing": "requires missing extensions",
            "self": "cannot require itself",
            "cycle": "dependency cycle",
        }
        for case, declarations in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "extensions"
                for name, requires in declarations:
                    self._runtime_extension(root, name, requires=requires)
                with self.assertRaisesRegex(
                    ExtensionConventionError, expected[case]
                ):
                    discover_extensions(root)

    def test_rejects_casefold_collisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "extensions"
            self._runtime_extension(root, "Alpha")
            self._runtime_extension(root, "alpha")
            if len(tuple(root.iterdir())) < 2:
                self.skipTest("filesystem is case-insensitive")
            with self.assertRaisesRegex(ExtensionConventionError, "name collision"):
                discover_extensions(root)

    @unittest.skipIf(os.name == "nt", "symlink policy requires POSIX test support")
    def test_rejects_symlinked_manifest_entrypoint_and_nested_content(self):
        targets = ("extension.yaml", "extension.py", "helpers/linked.py")
        for target in targets:
            with (
                self.subTest(target=target),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary) / "extensions"
                directory = self._runtime_extension(root, "unsafe")
                destination = directory / target
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    destination.unlink()
                destination.symlink_to(directory / "extension.py")
                with self.assertRaisesRegex(ExtensionConventionError, "symlink"):
                    discover_extensions(root)

    def test_digest_detects_post_discovery_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "extensions"
            directory = self._runtime_extension(root, "mutable")
            self._write(directory / "helpers.py", "VALUE = 1\n")
            descriptor = discover_extensions(root)[0]
            self.assertEqual(descriptor.digest, extension_digest(directory))

            self._write(directory / "helpers.py", "VALUE = 2\n")

            with self.assertRaisesRegex(ExtensionConventionError, "changed"):
                verify_extension(descriptor)

    def test_exported_capability_vocabulary_is_closed_and_dotted(self):
        self.assertIn("lifecycle.agent", EXTENSION_CAPABILITIES)
        self.assertIn("context.credentials", EXTENSION_CAPABILITIES)
        self.assertIn("context.continuations", EXTENSION_CAPABILITIES)
        self.assertIn("context.skills", EXTENSION_CAPABILITIES)
        self.assertIn("lifecycle.skills", EXTENSION_CAPABILITIES)
        self.assertIn("native.langgraph", EXTENSION_CAPABILITIES)
        self.assertIn("sandbox.provider", EXTENSION_CAPABILITIES)
        self.assertTrue(all("." in value for value in EXTENSION_CAPABILITIES))


if __name__ == "__main__":
    unittest.main()
