"""Expose a Harnest sandbox to LangGraph as a lazy, typed execution tool, and scope native graph nodes."""

from __future__ import annotations

import functools
from typing import Any

from ...context_sandboxes import deny_sandbox_authority
from ...sandbox_adapters import SANDBOX_TOOL_NAME, _PortableToolRunner
from ...sandbox_graph_scope import deny_event_stream, deny_graph_callable


def sandbox_tool(definition: Any, *, name: str = SANDBOX_TOOL_NAME) -> Any:
    """Expose isolated Python through LangGraph's existing native tool loop."""
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel, ConfigDict, Field

    class Arguments(BaseModel):
        """Reject undeclared authority and non-text model arguments."""

        model_config = ConfigDict(extra="forbid", strict=True)
        code: str = Field(min_length=1)

    runner = _PortableToolRunner(definition, "langgraph")

    def execute(code: str) -> dict[str, Any]:
        """Run Python in the configured provider, not the agent server process."""
        return runner.execute(code)

    async def aexecute(code: str) -> dict[str, Any]:
        """Preserve invocation context while keeping blocking SDKs off the loop."""
        return await runner.aexecute(code)

    return StructuredTool.from_function(
        func=execute, coroutine=aexecute, name=name,
        description=f"Execute Python in the assigned sandbox {name}. "
        "Use print() to return output; results include stdout, stderr, and output_files.",
        args_schema=Arguments,
    )


def deny_langgraph_node(target: Any) -> Any:
    """Protect native Pregel calls without replacing its checkpoint semantics."""
    if getattr(target, "_harnest_sandbox_graph_scoped", False):
        return target
    for name in ("invoke", "ainvoke"):
        object.__setattr__(target, name, deny_graph_callable(getattr(target, name)))
    original_async = target.astream
    original_sync = target.stream

    @functools.wraps(original_async)
    async def astream(*args: Any, **kwargs: Any) -> Any:
        """Protect native streaming while preserving the caller's scope."""
        with deny_sandbox_authority():
            events = deny_event_stream(original_async(*args, **kwargs))
        try:
            async for event in events:
                yield event
        finally:
            await events.aclose()

    @functools.wraps(original_sync)
    def stream(*args: Any, **kwargs: Any) -> Any:
        """Apply the same boundary to synchronous graph consumption."""
        with deny_sandbox_authority():
            events = original_sync(*args, **kwargs)
        try:
            while True:
                with deny_sandbox_authority():
                    try:
                        event = next(events)
                    except StopIteration:
                        break
                yield event
        finally:
            with deny_sandbox_authority():
                events.close()

    object.__setattr__(target, "astream", astream)
    object.__setattr__(target, "stream", stream)
    object.__setattr__(target, "_harnest_sandbox_graph_scoped", True)
    return target
