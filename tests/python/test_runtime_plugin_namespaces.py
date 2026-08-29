import asyncio
import importlib
from pathlib import Path
import sys
import tempfile
import unittest

import harnest.plugins as plugin_namespace
from harnest.plugins import (
    PluginContext,
    PluginContextUnavailableError,
    PluginImportError,
    PluginNamespaceError,
    PluginStartContext,
    activate_runtime_plugins,
    release_runtime_plugins,
    runtime_plugin_namespaces,
)
from harnest.runtime_plugins import (
    RuntimePluginConventionError,
    discover_runtime_plugins,
)


class RuntimePluginNamespaceTests(unittest.TestCase):
    def _write(self, path: Path, contents: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    def _plugin(
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
            "kind: RuntimePlugin",
            "metadata:",
            f"  name: {name}",
            "  version: 1.0.0",
            "runtime:",
            "  entrypoint: plugin:plugin",
        ]
        if requires:
            manifest.extend(("requires:", "  plugins:"))
            manifest.extend(f"    - {required}" for required in requires)
        self._write(directory / "plugin.yaml", "\n".join(manifest) + "\n")
        self._write(
            directory / "plugin.py",
            source
            or (
                "from harnest.plugins import Plugin\n"
                f"class {name.title()}Plugin(Plugin):\n"
                "    pass\n"
                f"plugin = {name.title()}Plugin()\n"
            ),
        )
        return directory

    def test_exposes_local_class_singleton_and_relative_imports(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugins"
            directory = self._plugin(
                root,
                "temporal",
                source=(
                    "from harnest.plugins import Plugin\n"
                    "from .helpers import VALUE\n"
                    "class TemporalPlugin(Plugin):\n"
                    "    def value(self): return VALUE\n"
                    "plugin = TemporalPlugin()\n"
                ),
            )
            self._write(directory / "helpers.py", "VALUE = 'ready'\n")
            descriptors = discover_runtime_plugins(root)

            with runtime_plugin_namespaces(descriptors) as activated:
                module = importlib.import_module("harnest.plugins.temporal")
                self.assertIs(module, activated[0].module)
                self.assertIs(module.plugin, activated[0].plugin)
                self.assertIs(type(module.plugin), module.TemporalPlugin)
                self.assertEqual(module.plugin.value(), "ready")
                self.assertIs(plugin_namespace.temporal, module)
                self.assertIn("harnest.plugins.temporal.helpers", sys.modules)

            self.assertNotIn("harnest.plugins.temporal", sys.modules)
            self.assertNotIn("harnest.plugins.temporal.helpers", sys.modules)
            self.assertFalse(hasattr(plugin_namespace, "temporal"))

    def test_dependency_namespace_is_ready_before_dependent_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugins"
            self._plugin(
                root,
                "alpha",
                source=(
                    "from harnest.plugins import Plugin\n"
                    "VALUE = 'alpha'\n"
                    "class AlphaPlugin(Plugin): pass\n"
                    "plugin = AlphaPlugin()\n"
                ),
            )
            self._plugin(
                root,
                "beta",
                requires=("alpha",),
                source=(
                    "from harnest.plugins import Plugin\n"
                    "from harnest.plugins.alpha import VALUE\n"
                    "class BetaPlugin(Plugin):\n"
                    "    dependency = VALUE\n"
                    "plugin = BetaPlugin()\n"
                ),
            )
            descriptors = discover_runtime_plugins(root)

            with runtime_plugin_namespaces(descriptors) as activated:
                self.assertEqual(
                    [item.descriptor.name for item in activated], ["alpha", "beta"]
                )
                self.assertEqual(activated[1].plugin.dependency, "alpha")

    def test_context_is_task_local_and_revocation_reaches_child_tasks(self):
        async def exercise(plugin):
            with self.assertRaises(PluginContextUnavailableError):
                _ = plugin.context
            context = PluginContext("temporal")
            self.assertIs(plugin.create_context(context), context)
            token = plugin._bind_context(context)
            release = asyncio.Event()

            async def retained_child():
                await release.wait()
                return plugin.context

            task = asyncio.create_task(retained_child())
            await asyncio.sleep(0)
            self.assertIs(plugin.context, context)
            context._revoke()
            release.set()
            with self.assertRaises(PluginContextUnavailableError):
                await task
            with self.assertRaises(PluginContextUnavailableError):
                _ = plugin.context
            plugin._reset_context(token)
            with self.assertRaises(PluginContextUnavailableError):
                _ = plugin.context

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugins"
            self._plugin(root, "temporal")
            descriptors = discover_runtime_plugins(root)
            with runtime_plugin_namespaces(descriptors) as activated:
                asyncio.run(exercise(activated[0].plugin))

    def test_base_start_and_stop_hooks_are_async_noops(self):
        async def exercise(plugin):
            context = PluginStartContext(
                plugin_name="temporal",
                framework="langgraph",
                root_agent_name="root",
                _custom_stores={},
            )
            self.assertIsNone(await plugin.start(context))
            self.assertIsNone(await plugin.stop())

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugins"
            self._plugin(root, "temporal")
            descriptors = discover_runtime_plugins(root)
            with runtime_plugin_namespaces(descriptors) as activated:
                asyncio.run(exercise(activated[0].plugin))

    def test_same_plugin_set_is_reference_counted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugins"
            self._plugin(root, "shared")
            descriptors = discover_runtime_plugins(root)
            first = activate_runtime_plugins(descriptors)
            second = activate_runtime_plugins(descriptors)
            try:
                self.assertIs(first[0].module, second[0].module)
                release_runtime_plugins(descriptors)
                self.assertIn("harnest.plugins.shared", sys.modules)
            finally:
                release_runtime_plugins(descriptors)
            self.assertNotIn("harnest.plugins.shared", sys.modules)

    def test_competing_plugin_sets_cannot_share_one_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            first_root = workspace / "first" / "plugins"
            second_root = workspace / "second" / "plugins"
            self._plugin(first_root, "first")
            self._plugin(second_root, "second")
            first = discover_runtime_plugins(first_root)
            second = discover_runtime_plugins(second_root)
            activate_runtime_plugins(first)
            try:
                with self.assertRaisesRegex(
                    PluginNamespaceError, "another compiled agent"
                ):
                    activate_runtime_plugins(second)
            finally:
                release_runtime_plugins(first)

    def test_partial_import_failure_is_sanitized_and_transactional(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugins"
            self._plugin(root, "alpha")
            self._plugin(
                root,
                "broken",
                requires=("alpha",),
                source="raise RuntimeError('provider-secret-value')\n",
            )
            descriptors = discover_runtime_plugins(root)

            with self.assertRaises(PluginImportError) as failure:
                activate_runtime_plugins(descriptors)

            self.assertNotIn("provider-secret-value", str(failure.exception))
            self.assertNotIn("harnest.plugins.alpha", sys.modules)
            self.assertNotIn("harnest.plugins.broken", sys.modules)
            self.assertFalse(hasattr(plugin_namespace, "alpha"))
            self.assertFalse(hasattr(plugin_namespace, "broken"))

    def test_rejects_ambiguous_or_nonlocal_plugin_exports(self):
        cases = {
            "object": "plugin = object()\n",
            "base": "from harnest.plugins import Plugin\nplugin = Plugin()\n",
            "hidden": (
                "from harnest.plugins import Plugin\n"
                "class _Hidden(Plugin): pass\n"
                "plugin = _Hidden()\n"
            ),
            "extra": (
                "from harnest.plugins import Plugin\n"
                "class First(Plugin): pass\n"
                "class Second(Plugin): pass\n"
                "plugin = First()\n"
            ),
        }
        for name, source in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "plugins"
                self._plugin(root, name, source=source)
                descriptors = discover_runtime_plugins(root)
                with self.assertRaises(PluginImportError):
                    activate_runtime_plugins(descriptors)
                self.assertNotIn(f"harnest.plugins.{name}", sys.modules)

    def test_activation_rejects_content_changed_after_discovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugins"
            directory = self._plugin(root, "mutable")
            descriptors = discover_runtime_plugins(root)
            self._write(directory / "plugin.py", "plugin = object()\n")

            with self.assertRaisesRegex(RuntimePluginConventionError, "changed"):
                activate_runtime_plugins(descriptors)
            self.assertNotIn("harnest.plugins.mutable", sys.modules)


if __name__ == "__main__":
    unittest.main()
