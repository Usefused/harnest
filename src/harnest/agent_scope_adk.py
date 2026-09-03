"""Scope actual native ADK work to the managed agent that owns its capabilities."""

from typing import Any

from .context import activate_agent_scope


def managed_adk_agent_type(native_type: type) -> type:
    """Wrap native execution without leaking child authority across yielded events."""

    class ManagedScopeLlmAgent(native_type):
        """Keep callbacks, model work, and authored tools under their owning agent."""

        async def run_async(self, parent_context: Any) -> Any:
            """Re-enter scope per advancement so event consumers retain their own."""
            events = super().run_async(parent_context)
            try:
                while True:
                    with activate_agent_scope(self.name):
                        try:
                            event = await anext(events)
                        except StopAsyncIteration:
                            break
                    yield event
            finally:
                with activate_agent_scope(self.name):
                    await events.aclose()

    return ManagedScopeLlmAgent


def scope_native_adk_agent(target: Any) -> Any:
    """Deny inherited sandbox grants inside explicitly unmanaged native children."""
    from .context_sandboxes import deny_sandbox_authority

    original = getattr(target, "run_async", None)
    if not callable(original) or getattr(target, "_harnest_sandbox_scoped", False):
        return target

    async def run_async(parent_context: Any) -> Any:
        """Keep unmanaged native execution separate from parent capabilities."""
        events = original(parent_context)
        try:
            while True:
                with deny_sandbox_authority(), activate_agent_scope(target.name):
                    try:
                        event = await anext(events)
                    except StopAsyncIteration:
                        break
                yield event
        finally:
            with deny_sandbox_authority(), activate_agent_scope(target.name):
                await events.aclose()

    # Preserve the native instance and its lifecycle; scope only its operation.
    object.__setattr__(target, "run_async", run_async)
    object.__setattr__(target, "_harnest_sandbox_scoped", True)
    return target
