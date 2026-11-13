"""Expose a Harnest sandbox to LangGraph as a lazy, typed execution tool."""

from __future__ import annotations

from typing import Any

from ...sandbox_adapters import SANDBOX_TOOL_NAME, _PortableToolRunner


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
