import tempfile
import unittest
from pathlib import Path

from harnest.extension_loader import ExtensionDiscoveryError, discover_extensions


class ExtensionDiscoveryTests(unittest.TestCase):
    def test_discovers_arbitrary_nested_files_and_orders_shared_phases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "extensions"
            (root / "nested").mkdir(parents=True)
            (root / "zeta.py").write_text(
                "from harnest.lifecycle import lifecycle\n"
                "def helper(): return 'ignored'\n"
                "@lifecycle.before_invoke(order=10)\n"
                "def late(context, value): return value\n",
                encoding="utf-8",
            )
            (root / "nested" / "alpha.py").write_text(
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.before_invoke(order=10)\n"
                "def first(context, value): return value\n"
                "@lifecycle.before_invoke(order=-1)\n"
                "def earliest(context, value): return value\n",
                encoding="utf-8",
            )

            result = discover_extensions(root, framework="adk")

        self.assertEqual(
            [item.function_name for item in result.listeners],
            ["earliest", "first", "late"],
        )
        self.assertEqual(result.native, ())

    def test_undecorated_public_helpers_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "extensions"
            root.mkdir(parents=True)
            (root / "helpers.py").write_text("def parse(value): return value\n")
            result = discover_extensions(root, framework="langgraph")
        self.assertEqual(result.listeners, ())

    def test_adk_plugin_factory_is_explicit_and_validated(self):
        try:
            from google.adk.plugins.base_plugin import BasePlugin  # noqa: F401
        except ImportError:
            self.skipTest("google-adk is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "extensions"
            root.mkdir(parents=True)
            (root / "audit.py").write_text(
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.adk_plugin(order=4)\n"
                "def plugin():\n"
                "  from google.adk.plugins.base_plugin import BasePlugin\n"
                "  return BasePlugin(name='audit')\n",
                encoding="utf-8",
            )
            result = discover_extensions(root, framework="adk")
        self.assertEqual([item.name for item in result.native], ["audit"])

    def test_wrong_native_value_and_factory_arguments_are_rejected(self):
        for source, message in (
            (
                "@lifecycle.adk_plugin\ndef plugin(value): return value\n",
                "no arguments",
            ),
            ("@lifecycle.adk_plugin\ndef plugin(): return object()\n", "BasePlugin"),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "extensions"
                root.mkdir(parents=True)
                (root / "bad.py").write_text(
                    "from harnest.lifecycle import lifecycle\n" + source,
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ExtensionDiscoveryError, message):
                    discover_extensions(root, framework="adk")

    def test_public_non_python_resources_and_invalid_framework_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "extensions"
            root.mkdir(parents=True)
            (root / "notes.txt").write_text("not executable")
            with self.assertRaisesRegex(ExtensionDiscoveryError, "Python files"):
                discover_extensions(root, framework="adk")
        with self.assertRaisesRegex(ExtensionDiscoveryError, "unsupported"):
            discover_extensions("missing", framework="other")

    def test_opposite_framework_native_factory_fails_instead_of_being_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "extensions"
            root.mkdir(parents=True)
            (root / "native.py").write_text(
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.langgraph_middleware\n"
                "def middleware(): return object()\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ExtensionDiscoveryError, "targets langgraph"):
                discover_extensions(root, framework="adk")

    def test_portable_listener_signature_is_validated_at_compile_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "extensions"
            root.mkdir(parents=True)
            (root / "invalid.py").write_text(
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.on_event\n"
                "def event(context): return None\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ExtensionDiscoveryError, "exactly two"):
                discover_extensions(root, framework="adk")

    def test_decorated_function_alias_is_rejected_as_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "extensions"
            root.mkdir(parents=True)
            (root / "duplicate.py").write_text(
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.on_error\n"
                "def notify(context, error): pass\n"
                "also_notify = notify\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ExtensionDiscoveryError, "multiple names"):
                discover_extensions(root, framework="adk")


if __name__ == "__main__":
    unittest.main()
