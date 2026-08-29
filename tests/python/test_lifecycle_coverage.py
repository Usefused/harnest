import unittest

from harnest.lifecycle_coverage import (
    CoverageLevel,
    LifecycleCoverage,
    lifecycle_coverage,
)


class LifecycleCoverageTests(unittest.TestCase):
    def test_managed_mode_reports_full_portable_coverage(self):
        coverage = lifecycle_coverage("adk", "managed")

        self.assertIsInstance(coverage, LifecycleCoverage)
        self.assertEqual(coverage.tool, CoverageLevel.FULL)
        self.assertEqual(coverage.mcp, CoverageLevel.FULL)
        self.assertEqual(coverage.subagent, CoverageLevel.BEST_EFFORT)
        self.assertEqual(coverage.report()["framework"], "adk")
        with self.assertRaises(TypeError):
            coverage.report()["tool"] = "unavailable"  # type: ignore[index]

    def test_advanced_mode_reports_native_ownership_without_overclaiming(self):
        coverage = lifecycle_coverage("langgraph", "advanced")

        self.assertEqual(coverage.invocation, CoverageLevel.FULL)
        self.assertEqual(coverage.tool, CoverageLevel.WRAPPED_ONLY)
        self.assertEqual(coverage.mcp, CoverageLevel.WRAPPED_ONLY)
        self.assertEqual(coverage.subagent, CoverageLevel.BEST_EFFORT)
        self.assertEqual(coverage.checkpoint, CoverageLevel.FRAMEWORK_OWNED)

    def test_adapter_can_narrow_observed_coverage(self):
        coverage = lifecycle_coverage(
            "adk",
            "advanced",
            overrides={"model": CoverageLevel.UNAVAILABLE},
        )

        self.assertEqual(coverage.stage("model"), CoverageLevel.UNAVAILABLE)
        with self.assertRaises(KeyError):
            coverage.stage("unknown")
        with self.assertRaises(TypeError):
            coverage.with_overrides({"tool": "full"})  # type: ignore[dict-item]


if __name__ == "__main__":
    unittest.main()
