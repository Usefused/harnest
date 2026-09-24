import asyncio
import importlib
from pathlib import Path
import sys
import tempfile
import unittest

import harnest.extensions as extension_namespace
from harnest.extensions import (
    ExtensionContext,
    ExtensionContextUnavailableError,
    ExtensionImportError,
    ExtensionNamespaceError,
    activate_extensions,
    release_extensions,
    extension_namespaces,
)
from harnest.extension_descriptors import (
    ExtensionConventionError,
    discover_extensions,
)


class HarnestExtensionNamespaceTests(unittest.TestCase):
    def _write(self, path: Path, contents: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    def _extension(
        self,
        root: Path,
        name: str,
        *,
        requires: tuple[str, ...] = (),
        source: str | None = None,
    ) -> Path:
        directory = root / name
        manifest = [
            "apiVersion: harnest.dev/v1alpha1",
            "kind: Extension",
            "metadata:",
            f"  name: {name}",
            "  version: 1.0.0",
            "runtime:",
            "  entrypoint: extension:extension",
        ]
        if requires:
            manifest.extend(("requires:", "  extensions:"))
            manifest.extend(f"    - {required}" for required in requires)
        self._write(directory / "extension.yaml", "\n".join(manifest) + "\n")
        self._write(
            directory / "extension.py",
            source
            or (
                "from harnest.extensions import Extension\n"
                f"class {name.title()}Extension(Extension):\n"
                "    pass\n"
                f"extension = {name.title()}Extension()\n"
            ),
        )
        return directory

    def test_exposes_local_class_singleton_and_relative_imports(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "extensions"
            directory = self._extension(
                root,
                "temporal",
                source=(
                    "from harnest.extensions import Extension\n"
                    "from .helpers import VALUE\n"
                    "class TemporalExtension(Extension):\n"
                    "    def value(self): return VALUE\n"
                    "extension = TemporalExtension()\n"
                ),
            )
            self._write(directory / "helpers.py", "VALUE = 'ready'\n")
            descriptors = discover_extensions(root)

            with extension_namespaces(descriptors) as activated:
                module = importlib.import_module("harnest.extensions.temporal")
                self.assertIs(module, activated[0].module)
                self.assertIs(module.extension, activated[0].extension)
                self.assertIs(type(module.extension), module.TemporalExtension)
                self.assertEqual(module.extension.value(), "ready")
                self.assertIs(extension_namespace.temporal, module)
                self.assertIn("harnest.extensions.temporal.helpers", sys.modules)

            self.assertNotIn("harnest.extensions.temporal", sys.modules)
            self.assertNotIn("harnest.extensions.temporal.helpers", sys.modules)
            self.assertFalse(hasattr(extension_namespace, "temporal"))

    def test_dependency_namespace_is_ready_before_dependent_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "extensions"
            self._extension(
                root,
                "alpha",
                source=(
                    "from harnest.extensions import Extension\n"
                    "VALUE = 'alpha'\n"
                    "class AlphaExtension(Extension): pass\n"
                    "extension = AlphaExtension()\n"
                ),
            )
            self._extension(
                root,
                "beta",
                requires=("alpha",),
                source=(
                    "from harnest.extensions import Extension\n"
                    "from harnest.extensions.alpha import VALUE\n"
                    "class BetaExtension(Extension):\n"
                    "    dependency = VALUE\n"
                    "extension = BetaExtension()\n"
                ),
            )
            descriptors = discover_extensions(root)

            with extension_namespaces(descriptors) as activated:
                self.assertEqual(
                    [item.descriptor.name for item in activated], ["alpha", "beta"]
                )
                self.assertEqual(activated[1].extension.dependency, "alpha")

    def test_context_is_task_local_and_revocation_reaches_child_tasks(self):
        async def exercise(extension):
            with self.assertRaises(ExtensionContextUnavailableError):
                _ = extension.context
            context = ExtensionContext("temporal")
            self.assertIs(extension.create_context(context), context)
            token = extension._bind_context(context)
            release = asyncio.Event()

            async def retained_child():
                await release.wait()
                return extension.context

            task = asyncio.create_task(retained_child())
            await asyncio.sleep(0)
            self.assertIs(extension.context, context)
            context._revoke()
            release.set()
            with self.assertRaises(ExtensionContextUnavailableError):
                await task
            with self.assertRaises(ExtensionContextUnavailableError):
                _ = extension.context
            extension._reset_context(token)
            with self.assertRaises(ExtensionContextUnavailableError):
                _ = extension.context

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "extensions"
            self._extension(root, "temporal")
            descriptors = discover_extensions(root)
            with extension_namespaces(descriptors) as activated:
                asyncio.run(exercise(activated[0].extension))

    def test_competing_extension_sets_cannot_share_one_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            first_root = workspace / "first" / "extensions"
            second_root = workspace / "second" / "extensions"
            self._extension(first_root, "first")
            self._extension(second_root, "second")
            first = discover_extensions(first_root)
            second = discover_extensions(second_root)
            activate_extensions(first)
            try:
                with self.assertRaisesRegex(
                    ExtensionNamespaceError, "another compiled agent"
                ):
                    activate_extensions(second)
            finally:
                release_extensions(first)

    def test_partial_import_failure_is_sanitized_and_transactional(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "extensions"
            self._extension(root, "alpha")
            self._extension(
                root,
                "broken",
                requires=("alpha",),
                source="raise RuntimeError('provider-secret-value')\n",
            )
            descriptors = discover_extensions(root)

            with self.assertRaises(ExtensionImportError) as failure:
                activate_extensions(descriptors)

            self.assertNotIn("provider-secret-value", str(failure.exception))
            self.assertNotIn("harnest.extensions.alpha", sys.modules)
            self.assertNotIn("harnest.extensions.broken", sys.modules)
            self.assertFalse(hasattr(extension_namespace, "alpha"))
            self.assertFalse(hasattr(extension_namespace, "broken"))

    def test_rejects_ambiguous_or_nonlocal_extension_exports(self):
        cases = {
            "object": "extension = object()\n",
            "base": "from harnest.extensions import Extension\nextension = Extension()\n",
            "hidden": (
                "from harnest.extensions import Extension\n"
                "class _Hidden(Extension): pass\n"
                "extension = _Hidden()\n"
            ),
            "extra": (
                "from harnest.extensions import Extension\n"
                "class First(Extension): pass\n"
                "class Second(Extension): pass\n"
                "extension = First()\n"
            ),
        }
        for name, source in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "extensions"
                self._extension(root, name, source=source)
                descriptors = discover_extensions(root)
                with self.assertRaises(ExtensionImportError):
                    activate_extensions(descriptors)
                self.assertNotIn(f"harnest.extensions.{name}", sys.modules)

    def test_activation_rejects_content_changed_after_discovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "extensions"
            directory = self._extension(root, "mutable")
            self._write(directory / "helpers.py", "VALUE = 1\n")
            descriptors = discover_extensions(root)
            # Imported helpers are part of the sealed extension, too.
            self._write(directory / "helpers.py", "VALUE = 2\n")

            with self.assertRaisesRegex(ExtensionConventionError, "changed"):
                activate_extensions(descriptors)
            self.assertNotIn("harnest.extensions.mutable", sys.modules)


if __name__ == "__main__":
    unittest.main()
