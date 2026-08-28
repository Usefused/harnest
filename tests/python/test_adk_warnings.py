import logging
import unittest
import warnings

from harnest._adk_warnings import (
    suppress_adk_warnings,
    suppress_managed_transfer_cache_warning,
)


class ADKWarningTests(unittest.TestCase):
    def test_filters_only_selected_harnest_owned_warning(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with suppress_adk_warnings("resumability"):
                warnings.warn(
                    "[EXPERIMENTAL] ResumabilityConfig: This feature is experimental",
                    UserWarning,
                )
                warnings.warn(
                    "[EXPERIMENTAL] feature FeatureName.PLUGGABLE_AUTH is enabled.",
                    UserWarning,
                )
                warnings.warn("application warning", UserWarning)

        self.assertEqual(
            [str(item.message) for item in caught],
            [
                "[EXPERIMENTAL] feature FeatureName.PLUGGABLE_AUTH is enabled.",
                "application warning",
            ],
        )

    def test_managed_filter_preserves_other_adk_runner_warnings(self):
        logger = logging.getLogger("google_adk.google.adk.runners")
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture()
        previous_level = logger.level
        previous_propagate = logger.propagate
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        logger.propagate = False
        try:
            with suppress_managed_transfer_cache_warning():
                logger.warning(
                    'App "demo" can transfer between agents but has no '
                    "context_cache_config."
                )
                logger.warning("actionable runner warning")
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)
            logger.propagate = previous_propagate

        self.assertEqual(records, ["actionable runner warning"])


if __name__ == "__main__":
    unittest.main()
