"""Inject LangGraph's native tool and thread identities into durable callables."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Mapping
from typing import Annotated, Any

from ...durable import ResumeArtifact, _required_identity, _tool_name, is_durable_tool, framework_durable_call


def langgraph_durable_callable(value: Callable[..., Any]) -> Callable[..., Any]:
    """Inject LangGraph's native tool and thread identities into a callable."""

    if not is_durable_tool(value):
        return value
    try:
        from langchain_core.runnables import RunnableConfig
        from langchain_core.tools import InjectedToolCallId
    except ImportError as exc:  # pragma: no cover - optional runtime dependency
        raise RuntimeError(
            "durable LangGraph tools require langchain-core"
        ) from exc

    # LangChain currently treats the canonical name specially when it copies
    # the model ToolCall id into an injected argument. Fail during lowering if
    # authored input claimed that reserved framework boundary.
    call_id_name = "tool_call_id"
    if call_id_name in inspect.signature(value).parameters:
        raise TypeError("durable LangGraph tools reserve tool_call_id")
    config_name = _private_parameter_name(value, "_harnest_runnable_config")

    @functools.wraps(value)
    async def invoke(*args: Any, **kwargs: Any) -> Any:
        tool_call_id = kwargs.pop(call_id_name)
        config = kwargs.pop(config_name)
        artifact = ResumeArtifact(
            framework="langgraph",
            framework_invocation_id=_langgraph_thread_id(config),
            tool_call_id=_required_identity(tool_call_id, "tool_call_id"),
            tool_name=_tool_name(value),
        )
        with framework_durable_call(artifact):
            return await value(*args, **kwargs)

    signature = inspect.signature(value)
    injected = (
        inspect.Parameter(
            call_id_name,
            kind=inspect.Parameter.KEYWORD_ONLY,
            annotation=Annotated[str, InjectedToolCallId],
        ),
        inspect.Parameter(
            config_name,
            kind=inspect.Parameter.KEYWORD_ONLY,
            annotation=RunnableConfig,
        ),
    )
    invoke.__signature__ = signature.replace(  # type: ignore[attr-defined]
        parameters=_insert_keyword_parameters(signature, injected)
    )
    # LangChain reads type hints separately from inspect.signature when it
    # decides which arguments are runtime-injected and hidden from the model.
    invoke.__annotations__ = {
        **getattr(value, "__annotations__", {}),
        call_id_name: Annotated[str, InjectedToolCallId],
        config_name: RunnableConfig,
    }
    return invoke


def _langgraph_thread_id(config: Any) -> str:
    """Read the checkpoint thread used by LangGraph's Command(resume=...)."""

    configurable = config.get("configurable") if isinstance(config, Mapping) else None
    if not isinstance(configurable, Mapping):
        raise RuntimeError("durable LangGraph tool requires configurable thread_id")
    return _required_identity(configurable.get("thread_id"), "thread_id")

def _private_parameter_name(value: Callable[..., Any], preferred: str) -> str:
    """Choose a collision-free name for one framework-injected parameter."""

    existing = inspect.signature(value).parameters
    name = preferred
    while name in existing:
        name = "_" + name
    return name

def _insert_keyword_parameters(
    signature: inspect.Signature,
    injected: tuple[inspect.Parameter, ...],
) -> list[inspect.Parameter]:
    """Insert hidden keyword parameters before an authored **kwargs slot."""

    parameters = list(signature.parameters.values())
    for index, parameter in enumerate(parameters):
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return [*parameters[:index], *injected, *parameters[index:]]
    return [*parameters, *injected]
