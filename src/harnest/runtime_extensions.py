"""Portable lifecycle extension wrapper for runtime drivers."""

from __future__ import annotations

import inspect
from dataclasses import replace
from typing import Any, AsyncIterator, Mapping, Sequence

from .lifecycle import DROP_EVENT, LifecycleContext, LifecycleListener
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
        self, driver: RuntimeDriver, extensions: Sequence[LifecycleListener]
    ) -> None:
        if isinstance(extensions, (str, bytes)):
            raise TypeError("extensions must be a sequence")
        normalized = tuple(extensions)
        if any(not isinstance(item, LifecycleListener) for item in normalized):
            raise TypeError("extensions must contain only LifecycleListener values")
        self._driver = driver
        self._extensions = normalized

    @property
    def info(self) -> AgentInfo:
        return self._driver.info

    @property
    def extensions(self) -> tuple[LifecycleListener, ...]:
        return self._extensions

    def _listeners(self, phase: str) -> tuple[LifecycleListener, ...]:
        return tuple(item for item in self._extensions if item.phase == phase)

    async def create_session(
        self,
        *,
        session_id: str,
        user_id: str,
        state: Mapping[str, Any],
    ) -> SessionRecord:
        return await self._driver.create_session(
            session_id=session_id, user_id=user_id, state=state
        )

    async def get_session(
        self, *, session_id: str, user_id: str
    ) -> SessionRecord | None:
        return await self._driver.get_session(
            session_id=session_id, user_id=user_id
        )

    async def list_sessions(self, *, user_id: str) -> Sequence[SessionRecord]:
        return await self._driver.list_sessions(user_id=user_id)

    async def update_session(
        self,
        *,
        session_id: str,
        user_id: str,
        state_delta: Mapping[str, Any],
    ) -> SessionRecord | None:
        return await self._driver.update_session(
            session_id=session_id,
            user_id=user_id,
            state_delta=state_delta,
        )

    async def delete_session(self, *, session_id: str, user_id: str) -> bool:
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
        context = _context(self._driver, request)
        try:
            transformed_request = await self._before(context, request)
            with model_invocation_scope(context):
                result = await self._driver.invoke(transformed_request)
            if not isinstance(result, InvocationResult):
                raise ExtensionTransformError(
                    "wrapped runtime driver returned a non-InvocationResult value"
                )
            events: list[RuntimeEvent] = []
            for event in result.events:
                transformed_event = await self._event(context, event)
                if transformed_event is not DROP_EVENT:
                    events.append(transformed_event)
            return await self._after(
                context, _result_with_events(result, events)
            )
        except Exception as error:
            await self._notify_error(context, error)
            raise

    async def stream(
        self, request: InvocationRequest
    ) -> AsyncIterator[RuntimeEvent]:
        context = _context(self._driver, request)
        try:
            transformed_request = await self._before(context, request)
            events: list[RuntimeEvent] = []
            with model_invocation_scope(context):
                async for event in self._driver.stream(transformed_request):
                    transformed_event = await self._event(context, event)
                    if transformed_event is DROP_EVENT:
                        continue
                    events.append(transformed_event)
                    yield transformed_event
            await self._after_stream(
                context, _stream_result(transformed_request, events)
            )
        except Exception as error:
            await self._notify_error(context, error)
            raise

    async def close(self) -> None:
        await self._driver.close()


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
    "StreamingResultTransformationError",
    "lifecycle_authenticator",
]
