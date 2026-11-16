"""Copy the containing folder to extensions/starter_runtime to enable this extension.

List contributed directories under extension.yaml contributes and declare their
matching lifecycle, context, or content capabilities before use.
"""

from harnest.extensions import Extension


class StarterExtension(Extension):
    """Own application-lifetime resources without importing a framework."""

    async def start(self, start_context):
        """Acquire extension-owned clients here, never during module import."""

    async def stop(self):
        """Close only resources this extension acquired during startup."""


extension = StarterExtension()
