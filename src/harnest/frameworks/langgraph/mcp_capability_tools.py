"""Project MCP capability operations as LangChain structured tools."""

from __future__ import annotations

from typing import Any

from ...mcp_capability_tools import _check_names, capability_functions


def langgraph_capability_tools(configured: Any, server_name: str, existing: list[Any]) -> list[Any]:
    """Use the same public names and permission projection as remote LangGraph tools."""

    from langchain_core.tools import StructuredTool
    from ...agent_principal import attach_required_permissions

    functions = capability_functions(configured, framework="langgraph")
    _check_names(existing, functions, prefix=f"{server_name}_")
    tools = [StructuredTool.from_function(coroutine=operation, name=f"{server_name}_{operation.__name__}") for operation in functions]
    for tool, operation in zip(tools, functions):
        object.__setattr__(tool, "__harnest_mcp_capability__", operation.__name__)
        attach_required_permissions(tool, () if configured.permission is None else (configured.permission,))
    return tools
