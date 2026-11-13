"""Lower durable callables to ADK's native long-running tool boundary."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ...durable import (
    FrameworkDurableSuspended,
    ResumeArtifact,
    _required_identity,
    is_durable_tool,
    framework_durable_call,
)


def adk_durable_tool(value: Callable[..., Any]) -> Any:
    """Lower a durable callable to ADK's native long-running tool boundary."""

    if not is_durable_tool(value):
        return value
    try:
        from google.adk.tools import LongRunningFunctionTool
    except ImportError as exc:  # pragma: no cover - optional runtime dependency
        raise RuntimeError("durable ADK tools require google-adk") from exc

    class _HarnestLongRunningFunctionTool(LongRunningFunctionTool):
        """Bind ADK's persisted invocation identity around authored execution."""

        async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:
            artifact = _adk_resume_artifact(tool_context, self.name)
            with framework_durable_call(artifact):
                try:
                    return await super().run_async(args=args, tool_context=tool_context)
                except FrameworkDurableSuspended:
                    return None

    return _HarnestLongRunningFunctionTool(value)


def _adk_resume_artifact(tool_context: Any, tool_name: str) -> ResumeArtifact:
    """Read the two ADK identities required by a later FunctionResponse."""

    # ToolContext exposes these stable identities directly. Reading its private
    # InvocationContext would couple Harnest to ADK's session proxy internals.
    invocation_id = getattr(tool_context, "invocation_id", None)
    function_call_id = getattr(tool_context, "function_call_id", None)
    return ResumeArtifact(
        framework="adk",
        framework_invocation_id=_required_identity(invocation_id, "invocation_id"),
        tool_call_id=_required_identity(function_call_id, "function_call_id"),
        tool_name=tool_name,
    )
