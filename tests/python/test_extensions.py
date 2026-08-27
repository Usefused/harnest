import tempfile
import unittest
from pathlib import Path

from harnest.extension_loader import ExtensionDiscoveryError, discover_extensions


class ExtensionDiscoveryTests(unittest.TestCase):
    def test_discovers_portable_extensions_in_directory_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "extensions"
            for name in ("zeta", "alpha"):
                path = root / name
                path.mkdir(parents=True)
                (path / "lifecycle.py").write_text(
                    "from harnest.extension import Extension\n"
                    f"extension = Extension(name={name!r})\n",
                    encoding="utf-8",
                )
            (root / "empty").mkdir()

            result = discover_extensions(root, framework="langgraph")

        self.assertEqual([item.name for item in result], ["alpha", "zeta"])
        self.assertTrue(all(item.portable is not None for item in result))
        self.assertTrue(all(item.native is None for item in result))

    def test_only_imports_the_selected_native_module(self):
        try:
            from google.adk.plugins.base_plugin import BasePlugin  # noqa: F401
        except ImportError:
            self.skipTest("google-adk is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "extensions" / "audit"
            root.mkdir(parents=True)
            (root / "adk.py").write_text(
                "from google.adk.plugins.base_plugin import BasePlugin\n"
                "extension = BasePlugin(name='audit')\n",
                encoding="utf-8",
            )
            (root / "langgraph.py").write_text(
                "raise RuntimeError('wrong backend imported')\n",
                encoding="utf-8",
            )

            result = discover_extensions(root.parent, framework="adk")

        self.assertEqual(result[0].native.name, "audit")

    def test_discovers_langgraph_native_middleware(self):
        try:
            from langchain.agents.middleware import AgentMiddleware
        except ImportError:
            self.skipTest("langchain is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "extensions" / "guardrail"
            root.mkdir(parents=True)
            (root / "langgraph.py").write_text(
                "from langchain.agents.middleware import AgentMiddleware\n"
                "extension = AgentMiddleware()\n",
                encoding="utf-8",
            )

            result = discover_extensions(root.parent, framework="langgraph")

        self.assertIsInstance(result[0].native, AgentMiddleware)

    def test_contract_rejects_flat_files_and_wrong_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "extensions"
            root.mkdir()
            (root / "audit.py").write_text("extension = None\n", encoding="utf-8")
            with self.assertRaisesRegex(
                ExtensionDiscoveryError, "identifier directory"
            ):
                discover_extensions(root, framework="adk")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "extensions" / "audit"
            path.mkdir(parents=True)
            (path / "lifecycle.py").write_text(
                "from harnest.extension import Extension\n"
                "extension = Extension(name='different')\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ExtensionDiscoveryError, "named 'audit'"):
                discover_extensions(path.parent, framework="adk")

    def test_other_backend_only_extension_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "extensions" / "native_only"
            path.mkdir(parents=True)
            (path / "langgraph.py").write_text(
                "extension = object()\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ExtensionDiscoveryError, "no lifecycle.py"):
                discover_extensions(path.parent, framework="adk")


if __name__ == "__main__":
    unittest.main()
