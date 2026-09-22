"""Publish the decision registry for graph nodes and other managed components."""

from harnest import context, lifecycle
from harnest.lib.decision_resources import decision_registry


@lifecycle.resource
@context.provider("decisions")
def decisions():
    """Give Harnest ownership of the provider's startup and shutdown context."""
    return decision_registry()
