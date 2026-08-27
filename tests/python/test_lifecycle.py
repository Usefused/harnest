import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from harnest.bundle import BundleConventionError, compile_application
from harnest.extension import Extension
from harnest.runtime import _runtime_driver
from harnest.runtime_extensions import ExtensionRuntimeDriver


class ExtensionCompilerTests(unittest.TestCase):
    @staticmethod
    def _agent(root: Path) -> None:
        (root / "agent.py").write_text(
            "from harnest.agent import Agent\n"
            "root_agent = Agent(name='root', model='unused/model')\n",
            encoding="utf-8",
        )
        (root / "instructions.md").write_text("Answer.\n", encoding="utf-8")

    @staticmethod
    def _backend() -> SimpleNamespace:
        return SimpleNamespace(
            lower_managed=Mock(side_effect=lambda value, **_kwargs: value),
            wrap_managed=Mock(return_value=None),
        )

    def test_compiler_keeps_portable_extensions_on_application(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._agent(root)
            path = root / "extensions" / "history"
            path.mkdir(parents=True)
            (path / "lifecycle.py").write_text(
                "from harnest.extension import Extension\n"
                "extension = Extension(name='history')\n",
                encoding="utf-8",
            )
            backend = self._backend()
            with patch("harnest.bundle.get_backend", return_value=backend):
                application = compile_application(
                    root, entrypoint="agent:root_agent", framework="adk"
                )

        self.assertEqual([item.name for item in application.extensions], ["history"])
        backend.lower_managed.assert_called_once()
        backend.wrap_managed.assert_called_once_with(
            application.target, native_extensions=()
        )

    def test_compiler_forwards_selected_langgraph_middleware(self):
        try:
            from langchain.agents.middleware import AgentMiddleware
        except ImportError:
            self.skipTest("langchain is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._agent(root)
            path = root / "extensions" / "guardrail"
            path.mkdir(parents=True)
            (path / "langgraph.py").write_text(
                "from langchain.agents.middleware import AgentMiddleware\n"
                "extension = AgentMiddleware()\n",
                encoding="utf-8",
            )
            backend = self._backend()
            with patch("harnest.bundle.get_backend", return_value=backend):
                application = compile_application(
                    root, entrypoint="agent:root_agent", framework="langgraph"
                )

        middleware = backend.lower_managed.call_args.kwargs["native_extensions"]
        self.assertEqual(len(middleware), 1)
        self.assertIsInstance(middleware[0], AgentMiddleware)
        backend.wrap_managed.assert_called_once_with(
            application.target, native_extensions=middleware
        )

    def test_adk_native_extension_is_attached_to_official_app(self):
        try:
            from google.adk.plugins.base_plugin import BasePlugin  # noqa: F401
        except ImportError:
            self.skipTest("google-adk is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agent.py").write_text(
                "from harnest.graph import START, Edge, Event, Graph\n"
                "def respond(value):\n"
                "    return Event(message='ok')\n"
                "root_agent = Graph(name='root', nodes={'respond': respond}, "
                "edges=(Edge(START, 'respond'),))\n",
                encoding="utf-8",
            )
            path = root / "extensions" / "audit"
            path.mkdir(parents=True)
            (path / "adk.py").write_text(
                "from google.adk.plugins.base_plugin import BasePlugin\n"
                "extension = BasePlugin(name='audit')\n",
                encoding="utf-8",
            )

            application = compile_application(
                root, entrypoint="agent:root_agent", framework="adk"
            )

        self.assertEqual(
            [plugin.name for plugin in application.native_app.plugins], ["audit"]
        )

    def test_runtime_selects_backend_then_adds_portable_wrapper(self):
        application = SimpleNamespace(
            framework="adk",
            extensions=(Extension(name="history"),),
        )
        raw_driver = Mock()
        with patch("harnest.runtime_adk.ADKRuntimeDriver", return_value=raw_driver):
            driver = _runtime_driver(application)
        self.assertIsInstance(driver, ExtensionRuntimeDriver)
        self.assertIs(driver._driver, raw_driver)

    def test_advanced_mode_rejects_filesystem_extensions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agent.py").write_text(
                "from harnest.agent import Agent\n"
                "root_agent = Agent.advanced(object())\n",
                encoding="utf-8",
            )
            path = root / "extensions" / "history"
            path.mkdir(parents=True)
            (path / "lifecycle.py").write_text(
                "from harnest.extension import Extension\n"
                "extension = Extension(name='history')\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(BundleConventionError, "extensions/"):
                compile_application(
                    root,
                    entrypoint="agent:root_agent",
                    framework="adk",
                    mode="advanced",
                )


if __name__ == "__main__":
    unittest.main()
