"""Portable model-call lifecycle and framework-native adapters."""

from __future__ import annotations

import inspect
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from typing import Any, Awaitable, Callable, Sequence

from .lifecycle import (
    LifecycleListener,
    ModelCallRequest,
    ModelCallResponse,
    ModelLifecycleContext,
    ModelMessage,
)


class ModelLifecycleError(TypeError):
    """A portable model listener returned an invalid transformation."""


_ACTIVE_MODEL_CONTEXT: ContextVar[ModelLifecycleContext | None] = ContextVar(
    "harnest_model_lifecycle_context", default=None
)


@contextmanager
def model_invocation_scope(context: Any):
    """Bind neutral invocation identity to nested framework model callbacks."""

    value = ModelLifecycleContext(
        context.framework,
        agent_name=context.agent_name,
        invocation_id=context.invocation_id,
        user_id=context.user_id,
        session_id=context.session_id,
    )
    token = _ACTIVE_MODEL_CONTEXT.set(value)
    try:
        yield
    finally:
        _ACTIVE_MODEL_CONTEXT.reset(token)


def current_model_context(framework: str) -> ModelLifecycleContext:
    current = _ACTIVE_MODEL_CONTEXT.get()
    if current is None:
        return ModelLifecycleContext(framework)
    if current.framework != framework:
        raise RuntimeError("model lifecycle framework does not match invocation")
    return current


class ModelLifecyclePipeline:
    """Apply ordered neutral listeners around one framework model call."""

    def __init__(self, listeners: Sequence[LifecycleListener], *, framework: str) -> None:
        self._listeners = tuple(
            sorted(
                (
                    item
                    for item in listeners
                    if item.phase
                    in {"before_model", "after_model", "on_model_error"}
                ),
                key=lambda item: (
                    item.order,
                    item.relative_path,
                    item.line,
                    item.function_name,
                ),
            )
        )
        self._framework = framework

    async def run(
        self,
        request: ModelCallRequest,
        handler: Callable[[ModelCallRequest], Awaitable[ModelCallResponse]],
        *,
        context: ModelLifecycleContext | None = None,
    ) -> ModelCallResponse:
        call_context = context or ModelLifecycleContext(self._framework)
        try:
            current, short = await self._before(call_context, request)
            response = short or await handler(current)
            return await self._after(call_context, response)
        except BaseException as error:
            await self._notify(call_context, error)
            raise

    def run_sync(
        self,
        request: ModelCallRequest,
        handler: Callable[[ModelCallRequest], ModelCallResponse],
        *,
        context: ModelLifecycleContext | None = None,
    ) -> ModelCallResponse:
        call_context = context or ModelLifecycleContext(self._framework)
        try:
            current, short = self._before_sync(call_context, request)
            response = short or handler(current)
            return self._after_sync(call_context, response)
        except BaseException as error:
            self._notify_sync(call_context, error)
            raise

    async def _before(
        self, context: ModelLifecycleContext, request: ModelCallRequest
    ) -> tuple[ModelCallRequest, ModelCallResponse | None]:
        current = request
        for listener in self._phase("before_model"):
            value = await _resolve(listener.callback(context, current))
            current, short = _before_value(listener, current, value)
            if short is not None:
                return current, short
        return current, None

    def _before_sync(
        self, context: ModelLifecycleContext, request: ModelCallRequest
    ) -> tuple[ModelCallRequest, ModelCallResponse | None]:
        current = request
        for listener in self._phase("before_model"):
            value = _sync(listener.callback(context, current), listener)
            current, short = _before_value(listener, current, value)
            if short is not None:
                return current, short
        return current, None

    async def _after(
        self, context: ModelLifecycleContext, response: ModelCallResponse
    ) -> ModelCallResponse:
        current = response
        for listener in self._phase("after_model"):
            value = await _resolve(listener.callback(context, current))
            current = _after_value(listener, current, value)
        return current

    def _after_sync(
        self, context: ModelLifecycleContext, response: ModelCallResponse
    ) -> ModelCallResponse:
        current = response
        for listener in self._phase("after_model"):
            value = _sync(listener.callback(context, current), listener)
            current = _after_value(listener, current, value)
        return current

    async def _notify(
        self, context: ModelLifecycleContext, error: BaseException
    ) -> None:
        for listener in self._phase("on_model_error"):
            try:
                await _resolve(listener.callback(context, error))
            except BaseException:
                continue

    def _notify_sync(
        self, context: ModelLifecycleContext, error: BaseException
    ) -> None:
        for listener in self._phase("on_model_error"):
            try:
                _sync(listener.callback(context, error), listener)
            except BaseException:
                continue

    def _phase(self, name: str) -> tuple[LifecycleListener, ...]:
        return tuple(item for item in self._listeners if item.phase == name)


def portable_model_extension(
    listeners: Sequence[LifecycleListener], *, framework: str
) -> Any | None:
    """Create one native adapter only when portable model listeners exist."""

    selected = tuple(
        item
        for item in listeners
        if item.phase in {"before_model", "after_model", "on_model_error"}
    )
    if not selected:
        return None
    pipeline = ModelLifecyclePipeline(selected, framework=framework)
    return _adk_plugin(pipeline) if framework == "adk" else _langgraph_middleware(pipeline)


def _before_value(
    listener: LifecycleListener, current: ModelCallRequest, value: Any
) -> tuple[ModelCallRequest, ModelCallResponse | None]:
    if value is None:
        return current, None
    if isinstance(value, ModelCallRequest):
        return value, None
    if isinstance(value, ModelCallResponse):
        return current, value
    raise ModelLifecycleError(
        f"model listener {listener.identity} must return ModelCallRequest, "
        "ModelCallResponse, or None"
    )


def _after_value(
    listener: LifecycleListener, current: ModelCallResponse, value: Any
) -> ModelCallResponse:
    if value is None:
        return current
    if isinstance(value, ModelCallResponse):
        return value
    raise ModelLifecycleError(
        f"model listener {listener.identity} must return ModelCallResponse or None"
    )


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _sync(value: Any, listener: LifecycleListener) -> Any:
    if not inspect.isawaitable(value):
        return value
    closer = getattr(value, "close", None)
    if callable(closer):
        closer()
    raise RuntimeError(
        f"model listener {listener.identity} is async; use async model execution"
    )


def _adk_plugin(pipeline: ModelLifecyclePipeline) -> Any:
    from google.adk.plugins.base_plugin import BasePlugin

    class PortableModelPlugin(BasePlugin):
        def __init__(self) -> None:
            super().__init__(name="_harnest_portable_model_lifecycle")

        async def before_model_callback(self, *, callback_context: Any, llm_request: Any) -> Any:
            request = _adk_request(llm_request)

            async def handler(updated: ModelCallRequest) -> ModelCallResponse:
                _apply_adk_request(llm_request, request, updated)
                return ModelCallResponse("")

            updated, short = await pipeline._before(_adk_context(callback_context), request)
            if short is not None:
                return _adk_response(short)
            _apply_adk_request(llm_request, request, updated)
            return None

        async def after_model_callback(self, *, callback_context: Any, llm_response: Any) -> Any:
            original = _adk_response_view(llm_response)
            updated = await pipeline._after(_adk_context(callback_context), original)
            return None if updated == original else _apply_adk_response(llm_response, updated)

        async def on_model_error_callback(
            self, *, callback_context: Any, llm_request: Any, error: Exception
        ) -> Any:
            del llm_request
            await pipeline._notify(_adk_context(callback_context), error)
            return None

    return PortableModelPlugin()


def _adk_context(value: Any) -> ModelLifecycleContext:
    current = current_model_context("adk")
    updates = {
        name: getattr(value, name, None)
        for name in ("agent_name", "invocation_id", "user_id", "session_id")
        if getattr(current, name) is None and getattr(value, name, None) is not None
    }
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


def _langgraph_middleware(pipeline: ModelLifecyclePipeline) -> Any:
    from langchain.agents.middleware import AgentMiddleware

    class PortableModelMiddleware(AgentMiddleware):
        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            original = _langgraph_request(request)
            context = current_model_context("langgraph")
            try:
                updated_request, short = await pipeline._before(context, original)
                if short is not None:
                    return _langgraph_short_response(short)
                native = await handler(
                    _apply_langgraph_request(request, original, updated_request)
                )
                response = _langgraph_response_view(native)
                updated = await pipeline._after(context, response)
                return native if updated == response else _apply_langgraph_response(native, updated)
            except BaseException as error:
                await pipeline._notify(context, error)
                raise

        def wrap_model_call(self, request: Any, handler: Any) -> Any:
            original = _langgraph_request(request)
            context = current_model_context("langgraph")
            try:
                updated_request, short = pipeline._before_sync(context, original)
                if short is not None:
                    return _langgraph_short_response(short)
                native = handler(
                    _apply_langgraph_request(request, original, updated_request)
                )
                response = _langgraph_response_view(native)
                updated = pipeline._after_sync(context, response)
                return native if updated == response else _apply_langgraph_response(native, updated)
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


__all__ = [
    "ModelLifecycleError",
    "ModelLifecyclePipeline",
    "current_model_context",
    "model_invocation_scope",
    "portable_model_extension",
]
