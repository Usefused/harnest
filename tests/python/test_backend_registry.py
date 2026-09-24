import importlib.util
import unittest

from harnest.agent import Agent
from harnest.backends import (
    AdvancedBackendValidationError,
    UnknownBackendError,
    backend_names,
    get_backend,
)


class BackendRegistryTests(unittest.TestCase):
    def test_registry_has_only_the_two_supported_backends(self):
        self.assertEqual(backend_names(), ("adk", "langgraph"))
        self.assertEqual(get_backend("adk").name, "adk")
        self.assertEqual(get_backend("langgraph").name, "langgraph")
        with self.assertRaisesRegex(
            UnknownBackendError, "framework must be adk or langgraph"
        ):
            get_backend("unknown")

    def test_advanced_validation_rejects_objects_from_neither_framework(self):
        cases = (
            (
                "adk",
                importlib.util.find_spec("google.adk") is not None,
                "google-adk is not installed",
                "expects google.adk App",
            ),
            (
                "langgraph",
                importlib.util.find_spec("langgraph") is not None,
                "langgraph is not installed",
                "compiled langgraph Pregel",
            ),
        )
        for framework, available, reason, message in cases:
            with self.subTest(framework=framework):
                if not available:
                    self.skipTest(reason)
                with self.assertRaisesRegex(AdvancedBackendValidationError, message):
                    get_backend(framework).validate_advanced(
                        Agent.advanced(object()), fallback_name="fallback"
                    )

if __name__ == "__main__":
    unittest.main()
