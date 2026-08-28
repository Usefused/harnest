import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from harnest.application import CompiledApplication
from harnest.bundle import (
    BundleConventionError,
    _attach_advanced_native_extensions,
    compile_application,
)
from harnest.lifecycle import LifecycleListener
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

    def test_compiler_keeps_all_portable_listeners_on_application(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._agent(root)
            path = root / "extensions"
            path.mkdir()
            (path / "history.py").write_text(
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.before_invoke\n"
                "def before(context, value): return value\n"
                "@lifecycle.after_invoke\n"
                "def after(context, value): return value\n",
                encoding="utf-8",
            )
            backend = self._backend()
            with patch("harnest.bundle.get_backend", return_value=backend):
                application = compile_application(
                    root, entrypoint="agent:root_agent", framework="adk"
                )

        self.assertEqual(
            [item.phase for item in application.extensions],
            ["before_invoke", "after_invoke"],
        )
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
            path = root / "extensions"
            path.mkdir()
            (path / "guardrail.py").write_text(
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.langgraph_middleware\n"
                "def middleware():\n"
                "  from langchain.agents.middleware import AgentMiddleware\n"
                "  return AgentMiddleware()\n",
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

    def test_adk_plugin_factory_is_attached_to_official_app(self):
        try:
            from google.adk.plugins.base_plugin import BasePlugin  # noqa: F401
        except ImportError:
            self.skipTest("google-adk is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agent.py").write_text(
                "from harnest.graph import START, Edge, Event, Graph\n"
                "def respond(value): return Event(message='ok')\n"
                "root_agent = Graph(name='root', nodes={'respond': respond}, "
                "edges=(Edge(START, 'respond'),))\n",
                encoding="utf-8",
            )
            path = root / "extensions"
            path.mkdir()
            (path / "audit.py").write_text(
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.adk_plugin\n"
                "def plugin():\n"
                "  from google.adk.plugins.base_plugin import BasePlugin\n"
                "  return BasePlugin(name='audit')\n",
                encoding="utf-8",
            )
            application = compile_application(
                root, entrypoint="agent:root_agent", framework="adk"
            )
        self.assertEqual([plugin.name for plugin in application.native_app.plugins], ["audit"])

    def test_advanced_adk_app_keeps_existing_and_discovered_plugins(self):
        from google.adk.agents import BaseAgent
        from google.adk.apps import App
        from google.adk.plugins.base_plugin import BasePlugin

        target = BaseAgent(name="root")
        existing = BasePlugin(name="existing")
        discovered = BasePlugin(name="discovered")
        advanced = SimpleNamespace(
            name="root",
            target=target,
            native_app=App(name="root", root_agent=target, plugins=[existing]),
        )
        # The production value is a dataclass; use it here to exercise model-copy
        # preservation without compiling an unrelated advanced entrypoint.
        from harnest.backends import AdvancedBackendResult

        result = _attach_advanced_native_extensions(
            AdvancedBackendResult(
                name=advanced.name,
                target=advanced.target,
                native_app=advanced.native_app,
            ),
            (discovered,),
            framework="adk",
        )
        self.assertEqual(
            [plugin.name for plugin in result.native_app.plugins],
            ["existing", "discovered"],
        )

    def test_runtime_adds_portable_wrapper(self):
        callback = lambda _context, value: value
        listener = LifecycleListener("before_invoke", callback, 0, "a.py", 1, "a")
        application = CompiledApplication(
            name="root",
            framework="adk",
            mode="managed",
            target=object(),
            extensions=(listener,),
        )
        raw_driver = Mock()
        with patch("harnest.runtime_adk.ADKRuntimeDriver", return_value=raw_driver):
            driver = _runtime_driver(application)
        self.assertIsInstance(driver, ExtensionRuntimeDriver)
        self.assertIs(driver._driver, raw_driver)

    def test_advanced_mode_accepts_portable_extensions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agent.py").write_text(
                "from harnest.agent import Agent\n"
                "root_agent = Agent.advanced(object())\n",
                encoding="utf-8",
            )
            path = root / "extensions"
            path.mkdir()
            (path / "history.py").write_text(
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.on_error\n"
                "def notify(context, error): pass\n",
                encoding="utf-8",
            )
            backend = SimpleNamespace(
                validate_advanced=Mock(
                    return_value=SimpleNamespace(
                        name="root", target=object(), native_app=None
                    )
                )
            )
            with patch("harnest.bundle.get_backend", return_value=backend):
                application = compile_application(
                    root,
                    entrypoint="agent:root_agent",
                    framework="langgraph",
                    mode="advanced",
                )
        self.assertEqual([item.phase for item in application.extensions], ["on_error"])

    def test_advanced_langgraph_rejects_late_native_middleware(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agent.py").write_text(
                "from harnest.agent import Agent\nroot_agent = Agent.advanced(object())\n"
            )
            path = root / "extensions"
            path.mkdir()
            (path / "native.py").write_text(
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.langgraph_middleware\n"
                "def native():\n"
                "  from langchain.agents.middleware import AgentMiddleware\n"
                "  return AgentMiddleware()\n"
            )
            backend = SimpleNamespace(
                validate_advanced=Mock(
                    return_value=SimpleNamespace(
                        name="root", target=object(), native_app=None
                    )
                )
            )
            with patch("harnest.bundle.get_backend", return_value=backend):
                with self.assertRaisesRegex(BundleConventionError, "cannot attach"):
                    compile_application(
                        root,
                        entrypoint="agent:root_agent",
                        framework="langgraph",
                        mode="advanced",
                    )


if __name__ == "__main__":
    unittest.main()
