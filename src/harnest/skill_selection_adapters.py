"""Framework request adapters for shared decision-backed skill selection."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from .context_session import _selection_state
from .skill_selection import DecisionSkillSelector, SkillSelectionError, selected_instructions


def adk_selection_callback(selector: DecisionSkillSelector) -> Any:
    """Attach selected bodies to model input without writing them into session state."""
    async def before_model(callback_context: Any, llm_request: Any) -> None:
        """Use the active invocation's user content, not model or tool-generated arguments."""
        content = callback_context.user_content
        task = "\n".join(part.text for part in (getattr(content, "parts", None) or ())
                         if getattr(part, "text", None))
        instructions = await selected_instructions(selector, task, _selection_state(callback_context.state.to_dict()))
        if instructions:
            llm_request.append_instructions([instructions])

    return before_model


def selection_middleware(selector: DecisionSkillSelector | None) -> tuple[Any, ...]:
    """Leave ordinary LangGraph agents unchanged unless selection is explicitly configured."""
    if selector is None:
        return ()
    from langchain.agents.middleware import AgentMiddleware

    class SkillSelectionMiddleware(AgentMiddleware):
        """Augment the model request only; graph checkpoints retain their original state."""

        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            """Run shared selection inside the outer Harnest agent-scope middleware."""
            text = await _langgraph_selection(selector, request)
            return await handler(_with_instructions(request, text))

        def wrap_model_call(self, request: Any, handler: Any) -> Any:
            """Support native synchronous invocation without blocking a running event loop."""
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                text = asyncio.run(_langgraph_selection(selector, request))
                return handler(_with_instructions(request, text))
            raise SkillSelectionError("use asynchronous invocation for skill selection inside an event loop")

    return (SkillSelectionMiddleware(),)


async def _langgraph_selection(selector: DecisionSkillSelector, request: Any) -> str:
    """Separate the latest human task from optional application state and prior messages."""
    task = next((_text(message.content) for message in reversed(request.messages)
                 if getattr(message, "type", None) == "human"), "")
    state = _selection_state(request.state)
    return await selected_instructions(selector, task, state)


def _text(content: Any) -> str:
    """Use text parts only; do not send media, tool payloads or message metadata by default."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(part["text"] for part in content
                     if isinstance(part, Mapping) and isinstance(part.get("text"), str))


def _with_instructions(request: Any, text: str) -> Any:
    """Preserve existing system content and native metadata without mutating shared requests."""
    if not text:
        return request
    from langchain_core.messages import SystemMessage

    system = request.system_message
    if system is None:
        return request.override(system_message=SystemMessage(content=text))
    content = system.content
    combined = f"{content}\n\n{text}" if isinstance(content, str) else [*content, {"type": "text", "text": text}]
    return request.override(system_message=system.model_copy(update={"content": combined}))
