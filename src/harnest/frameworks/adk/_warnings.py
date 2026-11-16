"""Narrow warning filters for ADK features owned by Harnest integrations."""

from __future__ import annotations

import logging
import warnings
from collections.abc import Iterator
from contextlib import contextmanager


_PATTERNS = {
    "pluggable_auth": (
        r"\[EXPERIMENTAL\] feature FeatureName\.PLUGGABLE_AUTH is enabled\."
    ),
    "resumability": (
        r"\[EXPERIMENTAL\] ResumabilityConfig: This feature is experimental.*"
    ),
}


@contextmanager
def suppress_adk_warnings(*features: str) -> Iterator[None]:
    """Hide only non-actionable ADK warnings for features Harnest selected."""

    with warnings.catch_warnings():
        for feature in features:
            warnings.filterwarnings(
                "ignore",
                message=_PATTERNS[feature],
                category=UserWarning,
            )
        yield


class _ManagedTransferCacheFilter(logging.Filter):
    """Drop only ADK's non-actionable managed transfer cache warning."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not (
            record.name == "google_adk.google.adk.runners"
            and record.levelno == logging.WARNING
            and "can transfer between agents but has no context_cache_config"
            in message
        )


@contextmanager
def suppress_managed_transfer_cache_warning() -> Iterator[None]:
    """Hide ADK's provider-specific cache hint during managed runner creation."""

    logger = logging.getLogger("google_adk.google.adk.runners")
    warning_filter = _ManagedTransferCacheFilter()
    logger.addFilter(warning_filter)
    try:
        yield
    finally:
        logger.removeFilter(warning_filter)


__all__ = ["suppress_adk_warnings", "suppress_managed_transfer_cache_warning"]
