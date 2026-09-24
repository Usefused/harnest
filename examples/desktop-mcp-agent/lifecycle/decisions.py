"""Publish one runtime-owned Jev decision registry to each invocation."""

from harnest import context, lifecycle
from harnest.lib.decision_resources import decision_registry


@lifecycle.resource
@context.provider("decisions")
def decisions():
    """Let Harnest open and close the Jev client with the application."""
    return decision_registry()
