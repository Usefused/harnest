"""Portable model-call lifecycle and framework-specific adapters."""

from __future__ import annotations

from collections.abc import Iterator

import inspect
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Sequence

from .lifecycle import (
    LifecycleListener,
    ModelCallRequest,
    ModelCallResponse,
    ModelLifecycleContext,
)
from .lifecycle_transition import Finish, Next, UNCHANGED


class ModelLifecycleError(TypeError):
    """A portable model listener returned an invalid transformation."""


_ACTIVE_MODEL_CONTEXT: ContextVar[ModelLifecycleContext | None] = ContextVar(
    "harnest_model_lifecycle_context", default=None
)


@contextmanager
def model_invocation_scope(context: Any) -> Iterator[None]:
    """Bind neutral invocation identity to nested framework model callbacks."""

    value = ModelLifecycleContext(
        context.framework,
        agent_name=context.agent_name,
        invocation_id=context.invocation_id,
        user_id=context.user_id,
        session_id=context.session_id,
    )
    from .tokens import _invocation_token_state

    token = _ACTIVE_MODEL_CONTEXT.set(value)
    try:
        with _invocation_token_state(context):
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
            current, finished = _after_value(listener, current, value)
            if finished:
                break
        return current

    def _after_sync(
        self, context: ModelLifecycleContext, response: ModelCallResponse
    ) -> ModelCallResponse:
        current = response
        for listener in self._phase("after_model"):
            value = _sync(listener.callback(context, current), listener)
            current, finished = _after_value(listener, current, value)
            if finished:
                break
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
    from .frameworks.registry import get_framework

    return get_framework(framework).model_lifecycle_extension(pipeline)


def bind_model_extension(extension: Any, *, agent_name: str) -> Any:
    """Bind a portable native adapter to one managed agent when supported."""

    binder = getattr(extension, "_harnest_bind_agent", None)
    return binder(agent_name) if callable(binder) else extension


def _before_value(
    listener: LifecycleListener, current: ModelCallRequest, value: Any
) -> tuple[ModelCallRequest, ModelCallResponse | None]:
    if isinstance(value, Finish):
        if isinstance(value.result, ModelCallResponse):
            return current, value.result
        raise ModelLifecycleError(
            f"model listener {listener.identity} must finish with ModelCallResponse"
        )
    if isinstance(value, Next):
        if value.value is UNCHANGED:
            return current, None
        if isinstance(value.value, ModelCallRequest):
            return value.value, None
        raise ModelLifecycleError(
            f"model listener {listener.identity} must continue with ModelCallRequest"
        )
    # Existing decorators keep their documented return contract while new
    # lifecycle namespaces can require explicit transitions after migration.
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
) -> tuple[ModelCallResponse, bool]:
    if isinstance(value, Finish):
        if isinstance(value.result, ModelCallResponse):
            return value.result, True
        raise ModelLifecycleError(
            f"model listener {listener.identity} must finish with ModelCallResponse"
        )
    if isinstance(value, Next):
        if value.value is UNCHANGED:
            return current, False
        if isinstance(value.value, ModelCallResponse):
            return value.value, False
        raise ModelLifecycleError(
            f"model listener {listener.identity} must continue with ModelCallResponse"
        )
    if value is None:
        return current, False
    if isinstance(value, ModelCallResponse):
        return value, False
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


__all__ = [
    "ModelLifecycleError",
    "ModelLifecyclePipeline",
    "current_model_context",
    "model_invocation_scope",
    "portable_model_extension",
    "bind_model_extension",
]
