"""Adapt portable model lifecycle hooks to one LangChain agent middleware."""

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


def _langgraph_middleware(
    pipeline: ModelLifecyclePipeline, *, agent_name: str | None = None
) -> Any:
    from langchain.agents.middleware import AgentMiddleware

    class PortableModelMiddleware(AgentMiddleware):
        def _harnest_bind_agent(self, value: str) -> Any:
            return _langgraph_middleware(pipeline, agent_name=value)

        def _context(self) -> ModelLifecycleContext:
            current = current_model_context("langgraph")
            return (
                replace(current, agent_name=agent_name)
                if agent_name is not None
                else current
            )

        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            with activate_agent_scope(agent_name):
                original = _langgraph_request(request)
                context = self._context()
                try:
                    updated_request, short = await pipeline._before(context, original)
                    if short is not None:
                        return _langgraph_short_response(short)
                    native = await handler(
                        _apply_langgraph_request(request, original, updated_request)
                    )
                    response = _langgraph_response_view(native)
                    updated = await pipeline._after(context, response)
                    return (
                        native
                        if updated == response
                        else _apply_langgraph_response(native, updated)
                    )
                except BaseException as error:
                    await pipeline._notify(context, error)
                    raise

        def wrap_model_call(self, request: Any, handler: Any) -> Any:
            with activate_agent_scope(agent_name):
                original = _langgraph_request(request)
                context = self._context()
                try:
                    updated_request, short = pipeline._before_sync(context, original)
                    if short is not None:
                        return _langgraph_short_response(short)
                    native = handler(
                        _apply_langgraph_request(request, original, updated_request)
                    )
                    response = _langgraph_response_view(native)
                    updated = pipeline._after_sync(context, response)
                    return (
                        native
                        if updated == response
                        else _apply_langgraph_response(native, updated)
                    )
                except BaseException as error:
                    pipeline._notify_sync(context, error)
                    raise

    return PortableModelMiddleware()

def _langgraph_request(value: Any) -> ModelCallRequest:
    model = getattr(value.model, "model_name", None) or getattr(value.model, "model", None)
    return ModelCallRequest(
        model=str(model) if model is not None else None,
        messages=tuple(
            ModelMessage(getattr(message, "type", "user"), _message_text(message))
            for message in value.messages
        ),
    )

def _apply_langgraph_request(value: Any, original: ModelCallRequest, updated: ModelCallRequest) -> Any:
    if updated.model != original.model:
        raise ModelLifecycleError("before_model cannot replace a LangGraph model by name")
    if len(updated.messages) != len(original.messages):
        raise ModelLifecycleError("before_model cannot add or remove LangGraph messages")
    messages = []
    for native, before, after in zip(value.messages, original.messages, updated.messages):
        if before.role != after.role:
            raise ModelLifecycleError("before_model cannot change LangGraph message roles")
        messages.append(
            native
            if before.text == after.text
            else _replace_langgraph_message_text(native, after.text)
        )
    if all(before is after for before, after in zip(value.messages, messages)):
        return value
    return value.override(messages=messages)

def _message_text(value: Any) -> str:
    content = getattr(value, "content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(_content_block_text(block) for block in content)

def _content_block_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"]
    return ""

def _replace_langgraph_message_text(message: Any, text: str) -> Any:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return message.model_copy(update={"content": text})
    if not isinstance(content, list):
        raise ModelLifecycleError("model hook cannot add text to a non-text LangGraph message")
    replacement = _replace_content_block_text(content, text)
    return message.model_copy(update={"content": replacement})

def _replace_content_block_text(content: list[Any], text: str) -> list[Any]:
    replaced = False
    result = []
    for block in content:
        current = _content_block_text(block)
        if not current and not _is_text_content_block(block):
            result.append(block)
            continue
        value = text if not replaced else ""
        result.append(_updated_content_block(block, value))
        replaced = True
    if not replaced:
        raise ModelLifecycleError("model hook cannot add text to a non-text LangGraph message")
    return result

def _is_text_content_block(value: Any) -> bool:
    return isinstance(value, str) or (
        isinstance(value, dict)
        and value.get("type") in {"text", "text_delta"}
        and "text" in value
    )

def _updated_content_block(value: Any, text: str) -> Any:
    if isinstance(value, str):
        return text
    return {**value, "text": text}

def _langgraph_response_view(value: Any) -> ModelCallResponse:
    return ModelCallResponse("".join(_message_text(item) for item in value.result))

def _langgraph_short_response(value: ModelCallResponse) -> Any:
    from langchain.agents.middleware.types import ModelResponse
    from langchain_core.messages import AIMessage

    return ModelResponse(result=[AIMessage(content=value.text)])

def _apply_langgraph_response(native: Any, updated: ModelCallResponse) -> Any:
    text_indices = [
        index
        for index, message in enumerate(native.result)
        if _message_has_text(message)
    ]
    if not text_indices:
        raise ModelLifecycleError("after_model cannot add text to a non-text LangGraph response")
    messages = list(native.result)
    first = text_indices[0]
    messages[first] = _replace_langgraph_message_text(messages[first], updated.text)
    for index in text_indices[1:]:
        messages[index] = _replace_langgraph_message_text(messages[index], "")
    return type(native)(result=messages, structured_response=native.structured_response)

def _message_has_text(value: Any) -> bool:
    content = getattr(value, "content", None)
    if isinstance(content, str):
        return True
    return isinstance(content, list) and any(
        _is_text_content_block(block) for block in content
    )
