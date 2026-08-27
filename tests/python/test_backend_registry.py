import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from harnest.agent import Agent, AgentDefinition
from harnest.backends import (
    AdvancedBackendValidationError,
    UnknownBackendError,
    backend_names,
    get_backend,
)
from harnest.bundle import compile_application
from harnest.graph import START, Edge, Graph


class BackendRegistryTests(unittest.TestCase):
    def test_registry_has_only_the_two_supported_backends(self):
        self.assertEqual(backend_names(), ("adk", "langgraph"))
        self.assertEqual(get_backend("adk").name, "adk")
        self.assertEqual(get_backend("langgraph").name, "langgraph")
        with self.assertRaisesRegex(
            UnknownBackendError, "framework must be adk or langgraph"
        ):
            get_backend("unknown")

    def test_managed_agent_and_graph_lowering_dispatch_through_registry(self):
        definition = AgentDefinition(
            name="root", model="unused/model", instruction="Answer."
        )
        graph = Graph(
            name="flow",
            nodes={"step": lambda value: value},
            edges=(Edge(START, "step"),),
        )
        adk_target = object()
        langgraph_target = object()

        with patch.object(
            AgentDefinition, "build", return_value=adk_target
        ) as adk_build:
            self.assertIs(get_backend("adk").lower_managed(definition), adk_target)
        adk_build.assert_called_once_with()

        with patch(
            "harnest.backends.langgraph.lower_graph",
            return_value=langgraph_target,
        ) as lower_graph:
            self.assertIs(
                get_backend("langgraph").lower_managed(graph),
                langgraph_target,
            )
        lower_graph.assert_called_once_with(graph, middleware=())

    def test_native_extensions_are_an_explicit_backend_input(self):
        validation_only = AgentDefinition(
            name="root", model="unused/model", instruction="Answer."
        )
        self.assertIsNone(get_backend("adk").wrap_managed(validation_only))
        self.assertIsNone(
            get_backend("langgraph").wrap_managed(
                object(), native_extensions=(object(),)
            )
        )

    @unittest.skipUnless(
        importlib.util.find_spec("google.adk") is not None,
        "google-adk is not installed",
    )
    def test_advanced_adk_validation_rejects_non_adk_objects(self):
        with self.assertRaisesRegex(
            AdvancedBackendValidationError, "expects google.adk App"
        ):
            get_backend("adk").validate_advanced(
                Agent.advanced(object()), fallback_name="fallback"
            )

    @unittest.skipUnless(
        importlib.util.find_spec("langgraph") is not None,
        "langgraph is not installed",
    )
    def test_advanced_langgraph_validation_rejects_non_pregel_objects(self):
        with self.assertRaisesRegex(
            AdvancedBackendValidationError, "compiled langgraph Pregel"
        ):
            get_backend("langgraph").validate_advanced(
                Agent.advanced(object()), fallback_name="fallback"
            )

    def test_bundle_selects_backend_once_then_uses_its_lowering_boundary(self):
        target = SimpleNamespace(name="compiled")
        backend = SimpleNamespace(
            lower_managed=Mock(return_value=target),
            wrap_managed=Mock(return_value=None),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agent.py").write_text(
                "from harnest.agent import Agent\n"
                "root_agent = Agent(name='root', model='unused/model')\n",
                encoding="utf-8",
            )
            (root / "instructions.md").write_text("Answer.\n", encoding="utf-8")
            with patch("harnest.bundle.get_backend", return_value=backend) as select:
                application = compile_application(
                    root, entrypoint="agent:root_agent", framework="adk"
                )

        select.assert_called_once_with("adk")
        backend.lower_managed.assert_called_once()
        lowered = backend.lower_managed.call_args.args[0]
        self.assertIsInstance(lowered, AgentDefinition)
        self.assertEqual(lowered.instruction, "Answer.")
        backend.lower_managed.assert_called_once_with(
            lowered, native_extensions=()
        )
        backend.wrap_managed.assert_called_once_with(
            target, native_extensions=()
        )
        self.assertIs(application.target, target)


if __name__ == "__main__":
    unittest.main()
