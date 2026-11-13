"""Adapt portable model lifecycle hooks to one ADK plugin."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ...context import activate_agent_scope
from ...lifecycle import (
    ModelCallRequest,
    ModelCallResponse,
    ModelLifecycleContext,
    ModelMessage,
)
from ...model_hooks import (
    ModelLifecycleError,
    ModelLifecyclePipeline,
    current_model_context,
)


def _adk_plugin(pipeline: ModelLifecyclePipeline) -> Any:
    from google.adk.plugins.base_plugin import BasePlugin

    class PortableModelPlugin(BasePlugin):
        def __init__(self) -> None:
            super().__init__(name="_harnest_portable_model_lifecycle")

        async def before_model_callback(self, *, callback_context: Any, llm_request: Any) -> Any:
            with activate_agent_scope(getattr(callback_context, "agent_name", None)):
                request = _adk_request(llm_request)

                async def handler(updated: ModelCallRequest) -> ModelCallResponse:
                    _apply_adk_request(llm_request, request, updated)
                    return ModelCallResponse("")

                updated, short = await pipeline._before(
                    _adk_context(callback_context), request
                )
                if short is not None:
                    return _adk_response(short)
                _apply_adk_request(llm_request, request, updated)
                return None

        async def after_model_callback(self, *, callback_context: Any, llm_response: Any) -> Any:
            with activate_agent_scope(getattr(callback_context, "agent_name", None)):
                original = _adk_response_view(llm_response)
                updated = await pipeline._after(
                    _adk_context(callback_context), original
                )
                return (
                    None
                    if updated == original
                    else _apply_adk_response(llm_response, updated)
                )

        async def on_model_error_callback(
            self, *, callback_context: Any, llm_request: Any, error: Exception
        ) -> Any:
            del llm_request
            with activate_agent_scope(getattr(callback_context, "agent_name", None)):
                await pipeline._notify(_adk_context(callback_context), error)
                return None

    return PortableModelPlugin()

def _adk_context(value: Any) -> ModelLifecycleContext:
    current = current_model_context("adk")
    updates: dict[str, Any] = {
        name: getattr(value, name, None)
        for name in ("invocation_id", "user_id", "session_id")
        if getattr(current, name) is None and getattr(value, name, None) is not None
    }
    # ADK's callback context identifies the agent executing this particular
    # model call. It must replace the invocation-level root name for managed
    # subagents, while authenticated invocation ownership remains fill-only.
    agent_name = getattr(value, "agent_name", None)
    if agent_name is not None:
        updates["agent_name"] = agent_name
    return replace(current, **updates) if updates else current

def _adk_request(value: Any) -> ModelCallRequest:
    return ModelCallRequest(
        model=getattr(value, "model", None),
        messages=tuple(
            ModelMessage(getattr(content, "role", "user") or "user", _parts_text(content))
            for content in value.contents
        ),
    )

def _apply_adk_request(value: Any, original: ModelCallRequest, updated: ModelCallRequest) -> None:
    if len(updated.messages) != len(original.messages):
        raise ModelLifecycleError("before_model cannot add or remove ADK messages")
    value.model = updated.model
    for content, before, after in zip(value.contents, original.messages, updated.messages):
        if before.role != after.role:
            raise ModelLifecycleError("before_model cannot change ADK message roles")
        if before.text != after.text:
            _replace_parts_text(content, after.text, phase="before_model")

def _parts_text(content: Any) -> str:
    return "".join(
        part.text for part in getattr(content, "parts", ()) if isinstance(getattr(part, "text", None), str)
    )

def _replace_parts_text(content: Any, text: str, *, phase: str) -> None:
    """Replace existing ADK text while preserving the responsible hook phase."""

    text_parts = [part for part in content.parts if isinstance(getattr(part, "text", None), str)]
    if not text_parts:
        raise ModelLifecycleError(
            f"{phase} cannot add text to a non-text ADK message"
        )
    text_parts[0].text = text
    for part in text_parts[1:]:
        part.text = ""

def _adk_response_view(value: Any) -> ModelCallResponse:
    return ModelCallResponse(_parts_text(getattr(value, "content", None)))

def _adk_response(value: ModelCallResponse) -> Any:
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types

    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=value.text)])
    )

def _apply_adk_response(native: Any, updated: ModelCallResponse) -> Any:
    replacement = native.model_copy(deep=True)
    if replacement.content is None:
        raise ModelLifecycleError("after_model cannot add text to an empty ADK response")
    _replace_parts_text(replacement.content, updated.text, phase="after_model")
    return replacement
