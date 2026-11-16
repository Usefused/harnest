"""Apply decision-based skill selection through an ADK before-model callback."""

from __future__ import annotations

from typing import Any

from ...context_session import _selection_state
from ...skill_selection import DecisionSkillSelector, selected_instructions


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
