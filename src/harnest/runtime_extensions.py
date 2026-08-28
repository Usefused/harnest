"""Portable lifecycle extension wrapper for runtime drivers."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
import inspect
from dataclasses import replace
from typing import Any, AsyncIterator, Mapping, Sequence

from .lifecycle import DROP_EVENT, LifecycleContext, LifecycleListener
from .context import (
    AgentContext,
    ContextValue,
    activate_context,
    bind_resource,
    create_agent_context,
    revoke_context,
)
from .neutral_runtime import (
    AgentInfo,
    InvocationRequest,
    InvocationResult,
    RuntimeDriver,
    RuntimeEvent,
    SessionRecord,
)
from .runtime_auth import (
    AuthPrincipal,
    AuthenticationError,
    ConnectionContext,
)
from .model_hooks import model_invocation_scope


class ExtensionTransformError(TypeError):
    """Raised when a portable hook returns an invalid replacement value."""


class StreamingResultTransformationError(ExtensionTransformError):
    """Raised when ``after_invoke`` tries to replace a streamed result."""


class RuntimeResourceError(RuntimeError):
    """Raised when a runtime-only resource cannot be entered safely."""


def _validate_context_exports(
    extensions: Sequence[LifecycleListener], values: Sequence[ContextValue]
) -> None:
    """Defend the runtime boundary when compiled metadata is built manually."""

    if any(not isinstance(item, ContextValue) for item in values):
        raise TypeError("context_values must contain only ContextValue values")
    exported = tuple(
        item.context_name for item in extensions if item.context_name is not None
    )
    names = [item.name for item in values] + list(exported)
    if len(names) != len(set(names)):
        raise ValueError("context resources cannot contain duplicate names")
    unnamed = any(
        item.phase == "context" and item.context_name is None for item in extensions
    )
    if unnamed:
        raise ValueError("context provider listeners require a context name")


async def _resolve(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _context(driver: RuntimeDriver, request: InvocationRequest) -> LifecycleContext:
    framework = driver.info.framework
    if framework not in {"adk", "langgraph"}:
        raise ValueError(
            "runtime extensions require driver.info.framework to be 'adk' or "
            "'langgraph'"
        )
    return LifecycleContext(
        framework=framework,
        agent_name=driver.info.name,
        invocation_id=request.invocation_id,
        user_id=request.user_id,
        session_id=request.session_id,
        metadata=request.metadata,
    )


def _result_with_events(
    result: InvocationResult, events: Sequence[RuntimeEvent]
) -> InvocationResult:
    original_has_messages = any(
        event.get("type") == "message" for event in result.events
    )
    text = result.text
    if original_has_messages:
        text = "".join(
            str(event.get("text", ""))
            for event in events
            if event.get("type") == "message"
        )

    output_events = [event for event in events if _is_output_event(event)]
    original_has_output = any(
        event.get("type") in {"graph_output", "output"}
        for event in result.events
    )
    value = result.result
    if output_events:
        value = _output_event_value(output_events[-1])
    elif original_has_output:
        value = None
    return replace(result, text=text, events=tuple(events), result=value)


def _is_output_event(event: RuntimeEvent) -> bool:
    return event.get("type") in {"graph_output", "output"}


def _output_event_value(event: RuntimeEvent) -> Any:
    if "result" in event:
        return event["result"]
    return event.get("output" if event.get("type") == "graph_output" else "value")


def _stream_result(
    request: InvocationRequest, events: Sequence[RuntimeEvent]
) -> InvocationResult:
    text = "".join(
        str(event.get("text", ""))
        for event in events
        if event.get("type") == "message"
    )
    value: Any = None
    for event in reversed(events):
        if not _is_output_event(event):
            continue
        value = _output_event_value(event)
        break
    return InvocationResult(
        text=text,
        events=tuple(events),
        result=value,
        session_id=request.session_id,
        metadata=request.metadata,
    )


class ExtensionRuntimeDriver(RuntimeDriver):
    """Decorate one backend driver with framework-neutral lifecycle hooks."""

    def __init__(
        self,
        driver: RuntimeDriver,
        extensions: Sequence[LifecycleListener],
        *,
        context_values: Sequence[ContextValue] = (),
    ) -> None:
        if isinstance(extensions, (str, bytes)):
            raise TypeError("extensions must be a sequence")
        normalized = tuple(extensions)
        if any(not isinstance(item, LifecycleListener) for item in normalized):
            raise TypeError("extensions must contain only LifecycleListener values")
        values = tuple(context_values)
        _validate_context_exports(normalized, values)
        self._driver = driver
        self._extensions = normalized
        self._context_values = values
        self._application_resources: dict[str, Any] = {}
        self._resource_lock = asyncio.Lock()
        self._resource_stack: AsyncExitStack | None = None
        self._closed = False

    @property
    def info(self) -> AgentInfo:
        return self._driver.info

    @property
    def extensions(self) -> tuple[LifecycleListener, ...]:
        return self._extensions

    def _listeners(self, phase: str) -> tuple[LifecycleListener, ...]:
        return tuple(item for item in self._extensions if item.phase == phase)

    async def _start_resources(self) -> None:
        """Enter resources once without calling their factories at compile time."""

        if self._resource_stack is not None:
            return
        async with self._resource_lock:
            if self._resource_stack is not None:
                return
            if self._closed:
                raise RuntimeError("extension runtime is closed")
            stack = AsyncExitStack()
            resources = {item.name: item.value for item in self._context_values}
            try:
                for listener in self._listeners("resource"):
                    value = await _enter_resource(stack, listener)
                    if listener.context_name is not None:
                        resources[listener.context_name] = value
            except BaseException as failure:
                try:
                    await stack.aclose()
                except BaseException as cleanup:
                    failure.add_note(
                        "partial runtime resource cleanup also failed with "
                        f"{type(cleanup).__name__}"
                    )
                raise
            self._resource_stack = stack
            self._application_resources = resources

    async def create_session(
        self,
        *,
        session_id: str,
        user_id: str,
        state: Mapping[str, Any],
    ) -> SessionRecord:
        await self._start_resources()
        return await self._driver.create_session(
            session_id=session_id, user_id=user_id, state=state
        )

    async def get_session(
        self, *, session_id: str, user_id: str
    ) -> SessionRecord | None:
        await self._start_resources()
        return await self._driver.get_session(
            session_id=session_id, user_id=user_id
        )

    async def list_sessions(self, *, user_id: str) -> Sequence[SessionRecord]:
        await self._start_resources()
        return await self._driver.list_sessions(user_id=user_id)

    async def update_session(
        self,
        *,
        session_id: str,
        user_id: str,
        state_delta: Mapping[str, Any],
    ) -> SessionRecord | None:
        await self._start_resources()
        return await self._driver.update_session(
            session_id=session_id,
            user_id=user_id,
            state_delta=state_delta,
        )

    async def delete_session(self, *, session_id: str, user_id: str) -> bool:
        await self._start_resources()
        return await self._driver.delete_session(
            session_id=session_id, user_id=user_id
        )

    async def _before(
        self, context: LifecycleContext, request: InvocationRequest
    ) -> InvocationRequest:
        current = request
        for listener in self._listeners("before_invoke"):
            hook = listener.callback
            replacement = await _resolve(hook(context, current))
            if replacement is None:
                continue
            if not isinstance(replacement, InvocationRequest):
                raise ExtensionTransformError(
                    f"extension {listener.identity} before_invoke must return "
                    "InvocationRequest or None"
                )
            if (
                replacement.invocation_id,
                replacement.user_id,
                replacement.session_id,
            ) != (current.invocation_id, current.user_id, current.session_id):
                raise ExtensionTransformError(
                    f"extension {listener.identity} before_invoke cannot replace "
                    "invocation_id, user_id, or session_id"
                )
            current = replacement
        return current

    async def _event(
        self, context: LifecycleContext, event: RuntimeEvent
    ) -> RuntimeEvent | object:
        current = event
        for listener in self._listeners("on_event"):
            hook = listener.callback
            replacement = await _resolve(hook(context, current))
            if replacement is DROP_EVENT:
                return DROP_EVENT
            if replacement is None:
                continue
            if not isinstance(replacement, dict):
                raise ExtensionTransformError(
                    f"extension {listener.identity} on_event must return a runtime "
                    "event, DROP_EVENT, or None"
                )
            current = replacement
        return current

    async def _after(
        self, context: LifecycleContext, result: InvocationResult
    ) -> InvocationResult:
        current = result
        for listener in self._listeners("after_invoke"):
            hook = listener.callback
            replacement = await _resolve(hook(context, current))
            if replacement is None:
                continue
            if not isinstance(replacement, InvocationResult):
                raise ExtensionTransformError(
                    f"extension {listener.identity} after_invoke must return "
                    "InvocationResult or None"
                )
            current = replacement
        return current

    async def _after_stream(
        self, context: LifecycleContext, result: InvocationResult
    ) -> None:
        for listener in self._listeners("after_invoke"):
            hook = listener.callback
            replacement = await _resolve(hook(context, result))
            if replacement is not None:
                raise StreamingResultTransformationError(
                    f"extension {listener.identity} after_invoke cannot replace a "
                    "result after streamed events have been emitted"
                )

    async def _notify_error(
        self, context: LifecycleContext, error: BaseException
    ) -> None:
        for listener in self._listeners("on_error"):
            hook = listener.callback
            try:
                await _resolve(hook(context, error))
            except BaseException:
                # Error hooks are notifications and must never replace the
                # failure that caused them to run.
                continue

    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        await self._start_resources()
        lifecycle_context = _context(self._driver, request)
        agent_context = self._agent_context(request)
        try:
            with activate_context(agent_context):
                try:
                    await self._bind_invocation_resources(agent_context)
                    transformed_request = await self._before(lifecycle_context, request)
                    with model_invocation_scope(lifecycle_context):
                        result = await self._driver.invoke(transformed_request)
                    if not isinstance(result, InvocationResult):
                        raise ExtensionTransformError(
                            "wrapped runtime driver returned a non-InvocationResult value"
                        )
                    events: list[RuntimeEvent] = []
                    for event in result.events:
                        transformed_event = await self._event(lifecycle_context, event)
                        if transformed_event is not DROP_EVENT:
                            events.append(transformed_event)
                    return await self._after(
                        lifecycle_context, _result_with_events(result, events)
                    )
                except Exception as error:
                    await self._notify_error(lifecycle_context, error)
                    raise
        finally:
            # ContextVars are copied into child tasks, so resetting this task's
            # token alone cannot remove capabilities from work that outlives it.
            revoke_context(agent_context)

    async def stream(
        self, request: InvocationRequest
    ) -> AsyncIterator[RuntimeEvent]:
        await self._start_resources()
        lifecycle_context = _context(self._driver, request)
        agent_context = self._agent_context(request)
        iterator: AsyncIterator[RuntimeEvent] | None = None
        try:
            with activate_context(agent_context):
                await self._bind_invocation_resources(agent_context)
                transformed_request = await self._before(lifecycle_context, request)
            events: list[RuntimeEvent] = []
            iterator = self._driver.stream(transformed_request).__aiter__()
            while True:
                transformed_event = await self._next_stream_event(
                    iterator, lifecycle_context, agent_context
                )
                if transformed_event is _STREAM_END:
                    break
                if transformed_event is DROP_EVENT:
                    continue
                events.append(transformed_event)
                # The caller must not inherit agent capabilities while it handles
                # a frame; context is rebound only while advancing the backend.
                yield transformed_event
            with activate_context(agent_context):
                await self._after_stream(
                    lifecycle_context, _stream_result(transformed_request, events)
                )
        except Exception as error:
            with activate_context(agent_context):
                await self._notify_error(lifecycle_context, error)
            raise
        finally:
            try:
                if iterator is not None:
                    with activate_context(agent_context):
                        await _close_iterator(iterator)
            finally:
                revoke_context(agent_context)

    def _agent_context(self, request: InvocationRequest) -> AgentContext:
        """Give each invocation an isolated view over shared application values."""

        return create_agent_context(
            framework=self.info.framework or "",
            agent_name=self.info.name,
            invocation_id=request.invocation_id,
            user_id=request.user_id,
            session_id=request.session_id,
            metadata=request.metadata,
            resources=self._application_resources,
        )

    async def _bind_invocation_resources(self, active: AgentContext) -> None:
        """Resolve ordinary providers once after application resources are visible."""

        for listener in self._listeners("context"):
            try:
                value = await _resolve(listener.callback())
            except Exception as exc:
                raise RuntimeResourceError(
                    f"context provider {listener.identity} failed with "
                    f"{type(exc).__name__}"
                ) from exc
            if _managed_value(value):
                raise RuntimeResourceError(
                    f"context provider {listener.identity} returned a managed "
                    "resource; combine it with @lifecycle.resource"
                )
            bind_resource(active, listener.context_name or "", value)

    async def _next_stream_event(
        self,
        iterator: AsyncIterator[RuntimeEvent],
        lifecycle_context: LifecycleContext,
        agent_context: AgentContext,
    ) -> RuntimeEvent | object:
        """Advance native streaming only while invocation capabilities are bound."""

        with activate_context(agent_context), model_invocation_scope(
            lifecycle_context
        ):
            try:
                event = await anext(iterator)
            except StopAsyncIteration:
                return _STREAM_END
            return await self._event(lifecycle_context, event)

    async def close(self) -> None:
        """Close framework execution before unwinding runtime resources."""

        async with self._resource_lock:
            if self._closed:
                return
            self._closed = True
            stack = self._resource_stack
            self._resource_stack = None
            self._application_resources = {}
        failure: BaseException | None = None
        try:
            await self._driver.close()
        except BaseException as exc:
            failure = exc
        if stack is not None:
            try:
                await stack.aclose()
            except BaseException as exc:
                if failure is None:
                    failure = exc
                else:
                    failure.add_note(
                        f"runtime resource cleanup also failed with {type(exc).__name__}"
                    )
        if failure is not None:
            raise failure


async def _enter_resource(
    stack: AsyncExitStack, listener: LifecycleListener
) -> Any:
    """Invoke one runtime-only factory and enter its sync or async context manager."""

    try:
        manager = _resource_manager(listener.callback)
    except Exception as exc:
        raise RuntimeResourceError(
            f"runtime resource factory {listener.identity} failed with "
            f"{type(exc).__name__}"
        ) from exc
    if inspect.isawaitable(manager):
        closer = getattr(manager, "close", None)
        if callable(closer):
            closer()
        raise RuntimeResourceError(
            f"runtime resource factory {listener.identity} must return a context manager"
        )
    try:
        if hasattr(manager, "__aenter__") and hasattr(manager, "__aexit__"):
            return await stack.enter_async_context(manager)
        if hasattr(manager, "__enter__") and hasattr(manager, "__exit__"):
            return stack.enter_context(manager)
    except Exception as exc:
        raise RuntimeResourceError(
            f"runtime resource {listener.identity} failed to start with "
            f"{type(exc).__name__}"
        ) from exc
    raise RuntimeResourceError(
        f"runtime resource factory {listener.identity} must return a context manager; "
        f"got {type(manager).__name__}"
    )


def _resource_manager(callback: Any) -> Any:
    """Accept generator providers without requiring contextlib boilerplate."""

    if inspect.isasyncgenfunction(callback):
        return asynccontextmanager(callback)()
    if inspect.isgeneratorfunction(callback):
        return contextmanager(callback)()
    return callback()


def _managed_value(value: Any) -> bool:
    return (
        inspect.isgenerator(value)
        or inspect.isasyncgen(value)
        or (hasattr(value, "__enter__") and hasattr(value, "__exit__"))
        or (hasattr(value, "__aenter__") and hasattr(value, "__aexit__"))
    )


async def _close_iterator(iterator: AsyncIterator[Any]) -> None:
    closer = getattr(iterator, "aclose", None)
    if callable(closer):
        await closer()


_STREAM_END = object()


class LifecycleAuthenticator:
    """Resolve one connection through all ordered authentication listeners."""

    def __init__(self, listeners: Sequence[LifecycleListener]) -> None:
        self._listeners = tuple(
            item for item in listeners if item.phase == "authenticate"
        )
        if not self._listeners:
            raise ValueError("authentication lifecycle requires at least one listener")

    async def authenticate(self, connection: ConnectionContext) -> AuthPrincipal:
        principal: AuthPrincipal | None = None
        for listener in self._listeners:
            replacement = await _resolve(listener.callback(connection, principal))
            principal = _replace_principal(principal, replacement, listener)
        if principal is None:
            raise AuthenticationError()
        return principal


def lifecycle_authenticator(
    listeners: Sequence[LifecycleListener],
) -> LifecycleAuthenticator | None:
    """Build the application's single auth pipeline when hooks exist."""

    selected = tuple(item for item in listeners if item.phase == "authenticate")
    return LifecycleAuthenticator(selected) if selected else None


def _replace_principal(
    current: AuthPrincipal | None,
    replacement: Any,
    listener: LifecycleListener,
) -> AuthPrincipal | None:
    if replacement is None:
        return current
    if not isinstance(replacement, AuthPrincipal):
        raise TypeError(
            f"authentication listener {listener.identity} must return "
            "AuthPrincipal or None"
        )
    if current is not None and current.user_id != replacement.user_id:
        # Identity may be enriched but never swapped after downstream policy saw it.
        raise AuthenticationError("Authentication policy rejected identity", status_code=403)
    return replacement


__all__ = [
    "DROP_EVENT",
    "ExtensionRuntimeDriver",
    "ExtensionTransformError",
    "LifecycleAuthenticator",
    "RuntimeResourceError",
    "StreamingResultTransformationError",
    "lifecycle_authenticator",
]
