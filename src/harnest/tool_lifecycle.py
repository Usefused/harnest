"""Portable lifecycle execution around Harnest-managed tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import functools
import inspect
from types import MappingProxyType
from typing import Any, TypeVar

from .lifecycle import LifecycleListener
from .lifecycle_transition import Finish, Next, TransitionContext, UNCHANGED


F = TypeVar("F", bound=Callable[..., Any])
_PHASES = frozenset({"before_tool", "after_tool", "on_tool_error"})


class ToolLifecycleError(TypeError):
    """A tool interceptor returned invalid or ambiguous control flow."""


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    """Provider-neutral arguments for one managed tool call."""

    name: str
    args: tuple[Any, ...] = ()
    kwargs: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Freeze keyword arguments without serializing tool-owned values."""

        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("tool call name must be a non-empty string")
        if not isinstance(self.args, tuple):
            raise TypeError("tool call args must be a tuple")
        if not isinstance(self.kwargs, Mapping):
            raise TypeError("tool call kwargs must be a mapping")
        object.__setattr__(self, "kwargs", MappingProxyType(dict(self.kwargs)))


@dataclass(frozen=True, slots=True)
class ToolLifecycleContext(TransitionContext):
    """Stable invocation identity associated with one managed tool call."""

    framework: str
    agent_name: str
    invocation_id: str
    user_id: str
    session_id: str
    tool_name: str


class ToolLifecyclePipeline:
    """Apply explicit portable interceptors around one managed tool call."""

    def __init__(self, listeners: Sequence[LifecycleListener]) -> None:
        selected = (item for item in listeners if item.phase in _PHASES)
        self._listeners = tuple(sorted(selected, key=_listener_order))

    async def run(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
        context: ToolLifecycleContext,
    ) -> Any:
        """Run one asynchronous tool call and notify errors without masking them."""

        try:
            current, finished = await self._before(context, request)
            result = finished.result if finished is not None else await handler(current)
            return await self._after(context, result)
        except BaseException as error:
            from .durable import is_native_suspension

            # LangGraph interrupts are successful control flow whose checkpoint
            # must propagate untouched; reporting them as tool failures would
            # corrupt lifecycle audit semantics.
            if is_native_suspension(error):
                raise
            await self._notify(context, error)
            raise

    def run_sync(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
        context: ToolLifecycleContext,
    ) -> Any:
        """Run one synchronous tool call while rejecting asynchronous hooks."""

        try:
            current, finished = self._before_sync(context, request)
            result = finished.result if finished is not None else handler(current)
            return self._after_sync(context, result)
        except BaseException as error:
            self._notify_sync(context, error)
            raise

    async def _before(
        self, context: ToolLifecycleContext, request: ToolCallRequest
    ) -> tuple[ToolCallRequest, Finish[Any] | None]:
        current = request
        for listener in self._phase("before_tool"):
            transition = await _resolve(listener.callback(context, current))
            current, finished = _before_transition(listener, current, transition)
            if finished is not None:
                return current, finished
        return current, None

    def _before_sync(
        self, context: ToolLifecycleContext, request: ToolCallRequest
    ) -> tuple[ToolCallRequest, Finish[Any] | None]:
        current = request
        for listener in self._phase("before_tool"):
            transition = _sync(listener.callback(context, current), listener)
            current, finished = _before_transition(listener, current, transition)
            if finished is not None:
                return current, finished
        return current, None

    async def _after(self, context: ToolLifecycleContext, result: Any) -> Any:
        current = result
        for listener in self._phase("after_tool"):
            transition = await _resolve(listener.callback(context, current))
            current, finished = _after_transition(listener, current, transition)
            if finished:
                break
        return current

    def _after_sync(self, context: ToolLifecycleContext, result: Any) -> Any:
        current = result
        for listener in self._phase("after_tool"):
            transition = _sync(listener.callback(context, current), listener)
            current, finished = _after_transition(listener, current, transition)
            if finished:
                break
        return current

    async def _notify(
        self, context: ToolLifecycleContext, error: BaseException
    ) -> None:
        for listener in self._phase("on_tool_error"):
            try:
                await _resolve(listener.callback(context, error))
            except BaseException:
                continue

    def _notify_sync(
        self, context: ToolLifecycleContext, error: BaseException
    ) -> None:
        for listener in self._phase("on_tool_error"):
            try:
                _sync(listener.callback(context, error), listener)
            except BaseException:
                continue

    def _phase(self, phase: str) -> tuple[LifecycleListener, ...]:
        return tuple(item for item in self._listeners if item.phase == phase)


_ACTIVE_TOOL_PIPELINE: ContextVar[ToolLifecyclePipeline | None] = ContextVar(
    "harnest_tool_lifecycle_pipeline", default=None
)


@contextmanager
def tool_lifecycle_scope(listeners: Sequence[LifecycleListener]):
    """Bind root lifecycle policy so nested managed tool calls inherit it."""

    with _tool_lifecycle_pipeline_scope(ToolLifecyclePipeline(listeners)):
        yield


@contextmanager
def _tool_lifecycle_pipeline_scope(pipeline: ToolLifecyclePipeline):
    """Bind one prevalidated pipeline for repeated runtime invocations."""

    if not isinstance(pipeline, ToolLifecyclePipeline):
        raise TypeError("tool lifecycle pipeline must be ToolLifecyclePipeline")
    token = _ACTIVE_TOOL_PIPELINE.set(pipeline)
    try:
        yield
    finally:
        _ACTIVE_TOOL_PIPELINE.reset(token)


def wrap_lifecycle_tool(function: F) -> F:
    """Wrap an authored tool without changing its inspected public signature."""

    if getattr(function, "__harnest_tool_lifecycle_wrapped__", False):
        return function
    wrapped = _wrap_async_tool(function) if inspect.iscoroutinefunction(function) else _wrap_sync_tool(function)
    setattr(wrapped, "__harnest_tool_lifecycle_wrapped__", True)
    return wrapped


def _wrap_async_tool(function: F) -> F:
    """Route an asynchronous managed call through the active pipeline."""

    @functools.wraps(function)
    async def invoke(*args: Any, **kwargs: Any) -> Any:
        from .agent_principal import require_capability

        require_capability(invoke, name=function.__name__)
        pipeline = _ACTIVE_TOOL_PIPELINE.get()
        if pipeline is None:
            return await function(*args, **kwargs)
        request = ToolCallRequest(function.__name__, tuple(args), kwargs)

        async def handler(updated: ToolCallRequest) -> Any:
            return await function(*updated.args, **dict(updated.kwargs))

        return await pipeline.run(request, handler, _tool_context(request.name))

    return invoke  # type: ignore[return-value]


def _wrap_sync_tool(function: F) -> F:
    """Route a synchronous managed call through the active pipeline."""

    @functools.wraps(function)
    def invoke(*args: Any, **kwargs: Any) -> Any:
        from .agent_principal import require_capability

        require_capability(invoke, name=function.__name__)
        pipeline = _ACTIVE_TOOL_PIPELINE.get()
        if pipeline is None:
            return function(*args, **kwargs)
        request = ToolCallRequest(function.__name__, tuple(args), kwargs)
        handler = lambda updated: function(*updated.args, **dict(updated.kwargs))
        return pipeline.run_sync(request, handler, _tool_context(request.name))

    return invoke  # type: ignore[return-value]


def _tool_context(name: str) -> ToolLifecycleContext:
    """Derive tool ownership only from Harnest's revocable invocation context."""

    from .context import context

    active = context.current()
    return ToolLifecycleContext(
        framework=active.framework,
        agent_name=active.agent_name,
        invocation_id=active.invocation_id,
        user_id=active.user_id,
        session_id=active.session_id,
        tool_name=name,
    )


def _before_transition(
    listener: LifecycleListener, current: ToolCallRequest, value: Any
) -> tuple[ToolCallRequest, Finish[Any] | None]:
    """Require explicit flow for new tool interceptors to prevent accidental bypass."""

    if isinstance(value, Finish):
        return current, value
    if not isinstance(value, Next):
        raise _transition_error(listener, "context.next(...) or context.finish(...)")
    if value.value is UNCHANGED:
        return current, None
    if not isinstance(value.value, ToolCallRequest):
        raise _transition_error(listener, "context.next(ToolCallRequest)")
    return value.value, None


def _after_transition(
    listener: LifecycleListener, current: Any, value: Any
) -> tuple[Any, bool]:
    """Apply an explicit result replacement or stop the remaining after chain."""

    if isinstance(value, Finish):
        return value.result, True
    if not isinstance(value, Next):
        raise _transition_error(listener, "context.next(...) or context.finish(...)")
    return (current if value.value is UNCHANGED else value.value), False


def _transition_error(listener: LifecycleListener, expected: str) -> ToolLifecycleError:
    return ToolLifecycleError(
        f"tool listener {listener.identity} must return {expected}"
    )


def _listener_order(listener: LifecycleListener) -> tuple[Any, ...]:
    return (
        listener.order,
        listener.relative_path,
        listener.line,
        listener.function_name,
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
        f"tool listener {listener.identity} is async; use async tool execution"
    )


__all__ = [
    "ToolCallRequest",
    "ToolLifecycleContext",
    "ToolLifecycleError",
    "ToolLifecyclePipeline",
    "tool_lifecycle_scope",
    "wrap_lifecycle_tool",
]
