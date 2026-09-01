"""Adapt Harnest's neutral LangGraph runtime to the ADK eval protocol."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
import json
from collections.abc import AsyncIterator, Iterator, Mapping
import sys
from types import ModuleType
from typing import Any
import uuid

from ._json import json_value
from .runtime import _application_with_asset_store, _runtime_driver
from .runtime_contract import InvocationRequest, InvocationResult, RuntimeDriver
from .runtime_pipeline import start_runtime_pipeline


class LangGraphEvalInputError(ValueError):
    """An ADK eval input cannot be represented by the neutral text runtime."""


@asynccontextmanager
async def langgraph_eval_agent_module(
    application: Any, *, card: Mapping[str, Any] | None = None
) -> AsyncIterator[str]:
    """Start one LangGraph runtime and expose its adapter as an ADK agent module."""

    configured = _application_with_asset_store(application)
    driver = _runtime_driver(configured, card=card)
    try:
        # The driver owns every wrapper even when startup partially succeeds,
        # so close it from the same boundary that attempted startup.
        await start_runtime_pipeline(driver)
        with runtime_eval_agent_module(driver) as module_name:
            yield module_name
    finally:
        await driver.close()


@contextmanager
def runtime_eval_agent_module(driver: RuntimeDriver) -> Iterator[str]:
    """Expose an already-started neutral driver through ADK's eval protocol."""

    with _registered_eval_module(_langgraph_eval_agent(driver)) as module_name:
        yield module_name


def _langgraph_eval_agent(driver: RuntimeDriver) -> Any:
    """Build an ADK BaseAgent that delegates invocations to one neutral driver."""

    from google.adk.agents import BaseAgent

    initialized_sessions: set[tuple[str, str]] = set()
    session_lock = asyncio.Lock()

    class LangGraphEvalAgent(BaseAgent):
        async def _run_async_impl(self, ctx: Any) -> AsyncIterator[Any]:
            """Translate one ADK evaluation turn through the LangGraph runtime."""

            user_id = ctx.session.user_id
            session_id = ctx.session.id
            await _ensure_session(
                driver,
                initialized_sessions,
                session_lock,
                user_id=user_id,
                session_id=session_id,
                state=dict(ctx.session.state),
            )
            request = InvocationRequest(
                input=_text_eval_input(ctx.user_content),
                user_id=user_id,
                session_id=session_id,
                invocation_id=ctx.invocation_id,
                metadata={"harnest.eval.framework": "langgraph"},
                state_delta={},
                transport="eval",
            )
            result = await driver.invoke(request)
            for event in _adk_events(result, ctx.invocation_id, self.name):
                yield event

    return LangGraphEvalAgent(
        name="harnest_langgraph_eval",
        description="ADK evaluation adapter for a Harnest LangGraph application.",
    )


async def _ensure_session(
    driver: RuntimeDriver,
    initialized: set[tuple[str, str]],
    lock: asyncio.Lock,
    *,
    user_id: str,
    session_id: str,
    state: Mapping[str, Any],
) -> None:
    """Create each evaluator-owned session once despite concurrent eval cases."""

    key = (user_id, session_id)
    if key in initialized:
        return
    async with lock:
        if key in initialized:
            return
        await driver.create_session(
            session_id=session_id,
            user_id=user_id,
            state=state,
        )
        initialized.add(key)


def _text_eval_input(content: Any) -> str:
    """Extract portable text while rejecting silent multimodal data loss."""

    parts = tuple(getattr(content, "parts", ()) or ())
    unsupported = [part for part in parts if not _is_text_part(part)]
    if unsupported:
        raise LangGraphEvalInputError(
            "LangGraph eval invocations currently require text-only userContent"
        )
    text = "".join(getattr(part, "text", "") or "" for part in parts)
    if not text.strip():
        raise LangGraphEvalInputError(
            "LangGraph eval invocations require non-empty userContent text"
        )
    return text


def _is_text_part(part: Any) -> bool:
    """Recognize a GenAI text part without depending on its concrete version."""

    if getattr(part, "text", None) is None:
        return False
    payload = part.model_dump(exclude_none=True) if hasattr(part, "model_dump") else {}
    # Thought signatures accompany text in some SDK versions and are not a
    # second input modality, so they are safe to ignore at this boundary.
    return set(payload) <= {"text", "thought", "thought_signature"}


def _adk_events(
    result: InvocationResult, invocation_id: str, author: str
) -> tuple[Any, ...]:
    """Project neutral tool events and one canonical final response into ADK."""

    events = []
    for item in result.events:
        event = _adk_tool_event(item, invocation_id, author)
        if event is not None:
            events.append(event)
    events.append(_adk_final_event(result, invocation_id, author))
    return tuple(events)


def _adk_tool_event(
    item: Mapping[str, Any], invocation_id: str, author: str
) -> Any | None:
    """Translate one neutral tool event while ignoring presentation events."""

    from google.adk.events import Event
    from google.genai import types

    kind = item.get("type")
    if kind == "tool_call":
        call = types.FunctionCall(
            id=_optional_text(item.get("id")),
            name=str(item.get("name") or "unknown_tool"),
            args=_mapping_value(item.get("arguments")),
        )
        content = types.Content(role="model", parts=[types.Part(function_call=call)])
    elif kind == "tool_result":
        response = types.FunctionResponse(
            id=_optional_text(item.get("id")),
            name=str(item.get("name") or "unknown_tool"),
            response=_response_value(item.get("result")),
        )
        content = types.Content(
            role="user", parts=[types.Part(function_response=response)]
        )
    else:
        return None
    return Event(invocation_id=invocation_id, author=author, content=content)


def _adk_final_event(
    result: InvocationResult, invocation_id: str, author: str
) -> Any:
    """Emit exactly one final response for response and judge-based metrics."""

    from google.adk.events import Event
    from google.genai import types

    text = result.text.strip()
    if not text and result.result is not None:
        text = json.dumps(json_value(result.result), ensure_ascii=False)
    content = types.Content(role="model", parts=[types.Part(text=text)])
    return Event(invocation_id=invocation_id, author=author, content=content)


def _mapping_value(value: Any) -> dict[str, Any]:
    """Keep tool arguments object-shaped as required by GenAI FunctionCall."""

    normalized = json_value(value)
    return dict(normalized) if isinstance(normalized, Mapping) else {"value": normalized}


def _response_value(value: Any) -> dict[str, Any]:
    """Keep tool results object-shaped without discarding scalar results."""

    normalized = json_value(value)
    return dict(normalized) if isinstance(normalized, Mapping) else {"result": normalized}


def _optional_text(value: Any) -> str | None:
    """Normalize optional runtime identifiers for GenAI tool event models."""

    return None if value is None else str(value)


@contextmanager
def _registered_eval_module(agent: Any) -> Iterator[str]:
    """Register a private importable module for ADK's public evaluator API."""

    package_name = f"_harnest_langgraph_eval_{uuid.uuid4().hex}"
    module_name = f"{package_name}.agent"
    package = ModuleType(package_name)
    package.__path__ = []
    module = ModuleType(module_name)
    module.root_agent = agent
    sys.modules[package_name] = package
    sys.modules[module_name] = module
    try:
        yield module_name
    finally:
        sys.modules.pop(module_name, None)
        sys.modules.pop(package_name, None)


__all__ = [
    "LangGraphEvalInputError",
    "langgraph_eval_agent_module",
    "runtime_eval_agent_module",
]
