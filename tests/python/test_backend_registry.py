import importlib.util
import unittest

from harnest.agent import Agent
from harnest.frameworks import (
    AdvancedTargetError,
    UnknownFrameworkError,
    framework_names,
    get_framework,
)


class FrameworkRegistryTests(unittest.TestCase):
    def test_registry_has_only_the_two_supported_frameworks(self):
        self.assertEqual(framework_names(), ("adk", "langgraph"))
        self.assertEqual(get_framework("adk").name, "adk")
        self.assertEqual(get_framework("langgraph").name, "langgraph")
        with self.assertRaisesRegex(
            UnknownFrameworkError, "framework must be adk or langgraph"
        ):
            get_framework("unknown")

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
                with self.assertRaisesRegex(AdvancedTargetError, message):
                    get_framework(framework).validate_advanced(
                        Agent.advanced(object()), fallback_name="fallback"
                    )

if __name__ == "__main__":
    unittest.main()
