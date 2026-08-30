"""Portable lifecycle extension wrapper for runtime drivers."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager, nullcontext
import inspect
from dataclasses import replace
from typing import Any, AsyncIterator, Callable, Mapping, Sequence

from ._exception_notes import add_exception_note
from .lifecycle import DROP_EVENT, LifecycleContext, LifecycleListener
from .lifecycle_transition import Finish, Next, UNCHANGED
from .context import (
    AgentContext,
    ContextValue,
    activate_context,
    bind_resource,
    create_agent_context,
    revoke_context,
)
from .context_session import invocation_session_context
from .credentials import (
    CredentialProvider,
    CredentialProviderError,
    _activate_credential_provider,
)
from .runtime_contract import (
    AgentInfo,
    InvocationRequest,
    InvocationResult,
    RuntimeDriver,
    RuntimeEvent,
    SessionMessage,
    SessionRecord,
)
from .runtime_auth import (
    AuthPrincipal,
    AuthenticationError,
    ConnectionContext,
)
from .model_hooks import model_invocation_scope
from .session import SessionStore
from .tool_lifecycle import tool_lifecycle_scope


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


def _runtime_extensions(
    values: Sequence[LifecycleListener],
) -> tuple[LifecycleListener, ...]:
    """Freeze only compiler-created lifecycle listener values."""

    if isinstance(values, (str, bytes)):
        raise TypeError("extensions must be a sequence")
    normalized = tuple(values)
    if any(not isinstance(item, LifecycleListener) for item in normalized):
        raise TypeError("extensions must contain only LifecycleListener values")
    return normalized


def _validate_runtime_owners(
    credential_provider: CredentialProvider | None,
    manage_credential_provider: bool,
    plugin_bindings: Callable[[], Mapping[str, Any]] | None,
) -> None:
    """Validate private owner seams without entering authored resources."""

    if credential_provider is not None and not isinstance(
        credential_provider, CredentialProvider
    ):
        raise TypeError("credential_provider must implement CredentialProvider")
    if not isinstance(manage_credential_provider, bool):
        raise TypeError("manage_credential_provider must be boolean")
    if plugin_bindings is not None and not callable(plugin_bindings):
        raise TypeError("plugin_bindings must be callable")


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
        # Public cards may supply a human-facing display name. Lifecycle
        # routing and child ancestry must use the stable application identity.
        agent_name=driver.info.id,
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


def _next_or_legacy(value: Any) -> Any:
    """Normalize explicit flow while preserving existing ``None`` compatibility."""

    if value is None:
        return UNCHANGED
    if isinstance(value, Next):
        return value.value
    return value


def _replacement_request(
    listener: LifecycleListener,
    current: InvocationRequest,
    replacement: Any,
) -> InvocationRequest:
    """Protect authenticated invocation ownership across request replacement."""

    if not isinstance(replacement, InvocationRequest):
        raise ExtensionTransformError(
            f"extension {listener.identity} before_invoke must return "
            "InvocationRequest or None, or use explicit lifecycle control flow"
        )
    ownership = ("invocation_id", "user_id", "session_id")
    if any(getattr(replacement, name) != getattr(current, name) for name in ownership):
        raise ExtensionTransformError(
            f"extension {listener.identity} before_invoke cannot replace "
            "invocation_id, user_id, or session_id"
        )
    return replacement


def _finished_invocation(
    listener: LifecycleListener,
    request: InvocationRequest,
    transition: Finish[Any],
) -> InvocationResult:
    """Validate a short-circuit without permitting cross-session output."""

    result = _replacement_result(listener, transition.result)
    if result.session_id != request.session_id:
        raise ExtensionTransformError(
            f"extension {listener.identity} before_invoke cannot finish another session"
        )
    return result


def _replacement_result(
    listener: LifecycleListener, replacement: Any
) -> InvocationResult:
    """Require the complete portable result at invocation boundaries."""

    if not isinstance(replacement, InvocationResult):
        raise ExtensionTransformError(
            f"extension {listener.identity} must return InvocationResult control flow"
        )
    return replacement


class ExtensionRuntimeDriver(RuntimeDriver):
    """Decorate one backend driver with framework-neutral lifecycle hooks."""

    def __init__(
        self,
        driver: RuntimeDriver,
        extensions: Sequence[LifecycleListener],
        *,
        context_values: Sequence[ContextValue] = (),
        asset_stores: Mapping[str, Any] | None = None,
        custom_stores: Mapping[str, Any] | None = None,
        session_store: SessionStore | None = None,
        credential_provider: CredentialProvider | None = None,
        manage_credential_provider: bool = True,
        plugin_bindings: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        """Validate application resources without acquiring runtime ownership."""

        normalized = _runtime_extensions(extensions)
        values = tuple(context_values)
        _validate_context_exports(normalized, values)
        _validate_runtime_owners(
            credential_provider, manage_credential_provider, plugin_bindings
        )
        self._driver = driver
        self._extensions = normalized
        self._context_values = values
        self._asset_stores = dict(asset_stores or {})
        self._custom_stores = dict(custom_stores or {})
        self._session_store = session_store
        self._credential_provider = credential_provider
        self._manage_credential_provider = manage_credential_provider
        self._plugin_bindings = plugin_bindings
        self._credential_provider_started = False
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

    async def start(self) -> None:
        """Eagerly enter credentials and resources for an application lifespan."""

        await self._start_resources()

    async def _start_resources(self) -> None:
        """Enter resources once without calling their factories at compile time."""

        if self._resource_stack is not None:
            return
        async with self._resource_lock:
            if self._resource_stack is not None:
                return
            if self._closed:
                raise RuntimeError("extension runtime is closed")
            await _start_wrapped_storage(self._driver)
            stack = AsyncExitStack()
            provider_started = await self._start_private_provider()
            try:
                resources = await self._enter_application_resources(stack)
            except BaseException as failure:
                await _unwind_resource_start(
                    stack,
                    self._credential_provider if provider_started else None,
                    failure,
                )
                raise
            self._resource_stack = stack
            self._application_resources = resources
            self._credential_provider_started = provider_started

    async def _start_private_provider(self) -> bool:
        """Start credentials only when this wrapper owns their lifecycle."""

        provider = self._credential_provider
        if provider is None or not self._manage_credential_provider:
            return False
        await _start_credential_provider(provider)
        return True

    async def _enter_application_resources(
        self, stack: AsyncExitStack
    ) -> dict[str, Any]:
        """Enter extension resources and collect only explicit context exports."""

        resources = {item.name: item.value for item in self._context_values}
        for listener in self._listeners("resource"):
            value = await _enter_resource(stack, listener)
            if listener.context_name is not None:
                resources[listener.context_name] = value
        return resources

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

    async def list_sessions(
        self,
        *,
        user_id: str,
        after: str | None = None,
        limit: int | None = None,
    ) -> Sequence[SessionRecord]:
        """Forward optional keyset bounds after starting application resources."""

        await self._start_resources()
        if after is None and limit is None:
            return await self._driver.list_sessions(user_id=user_id)
        return await self._driver.list_sessions(
            user_id=user_id, after=after, limit=limit
        )

    async def get_session_messages(
        self, *, session_id: str, user_id: str
    ) -> Sequence[SessionMessage] | None:
        """Start application resources before reading the native transcript."""

        await self._start_resources()
        return await self._driver.get_session_messages(
            session_id=session_id, user_id=user_id
        )

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
    ) -> tuple[InvocationRequest, InvocationResult | None]:
        current = request
        for listener in self._listeners("before_invoke"):
            hook = listener.callback
            value = await _resolve(hook(context, current))
            if isinstance(value, Finish):
                return current, _finished_invocation(listener, current, value)
            replacement = _next_or_legacy(value)
            if replacement is UNCHANGED:
                continue
            current = _replacement_request(listener, current, replacement)
        return current, None

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
            value = await _resolve(hook(context, current))
            if isinstance(value, Finish):
                return _replacement_result(listener, value.result)
            replacement = _next_or_legacy(value)
            if replacement is UNCHANGED:
                continue
            current = _replacement_result(listener, replacement)
        return current

    async def _after_stream(
        self, context: LifecycleContext, result: InvocationResult
    ) -> None:
        for listener in self._listeners("after_invoke"):
            hook = listener.callback
            replacement = await _resolve(hook(context, result))
            if replacement is None or (
                isinstance(replacement, Next) and replacement.value is UNCHANGED
            ):
                continue
            else:
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
            async with self._session_scope(request):
                with activate_context(agent_context), self._credential_scope():
                    return await self._invoke_bound(
                        request, lifecycle_context, agent_context
                    )
        finally:
            # ContextVars are copied into child tasks, so resetting this task's
            # token alone cannot remove capabilities from work that outlives it.
            revoke_context(agent_context)

    async def _invoke_bound(
        self,
        request: InvocationRequest,
        lifecycle_context: LifecycleContext,
        agent_context: AgentContext,
    ) -> InvocationResult:
        """Run every invocation hook while all scoped capabilities are active."""

        try:
            await self._bind_invocation_resources(agent_context)
            transformed_request, short = await self._before(
                lifecycle_context, request
            )
            with (
                model_invocation_scope(lifecycle_context),
                tool_lifecycle_scope(self._extensions),
            ):
                result = (
                    short
                    if short is not None
                    else await self._driver.invoke(transformed_request)
                )
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

    async def stream(
        self, request: InvocationRequest
    ) -> AsyncIterator[RuntimeEvent]:
        await self._start_resources()
        lifecycle_context = _context(self._driver, request)
        agent_context = self._agent_context(request)
        iterator: AsyncIterator[RuntimeEvent] | None = None
        try:
            async with self._session_scope(request):
                try:
                    with activate_context(agent_context), self._credential_scope():
                        await self._bind_invocation_resources(agent_context)
                        transformed_request, short = await self._before(
                            lifecycle_context, request
                        )
                    events: list[RuntimeEvent] = []
                    if short is not None:
                        async for event in self._short_stream(
                            short, lifecycle_context, agent_context, events
                        ):
                            yield event
                        return
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
                    with activate_context(agent_context), self._credential_scope():
                        await self._after_stream(
                            lifecycle_context,
                            _stream_result(transformed_request, events),
                        )
                except Exception as error:
                    with activate_context(agent_context), self._credential_scope():
                        await self._notify_error(lifecycle_context, error)
                    raise
                finally:
                    if iterator is not None:
                        with activate_context(
                            agent_context
                        ), self._credential_scope():
                            await _close_iterator(iterator)
        finally:
            revoke_context(agent_context)

    def _agent_context(self, request: InvocationRequest) -> AgentContext:
        """Give each invocation an isolated view over shared application values."""

        return create_agent_context(
            framework=self.info.framework or "",
            # The display name is transport metadata, not an execution identity.
            agent_name=self.info.id,
            invocation_id=request.invocation_id,
            user_id=request.user_id,
            session_id=request.session_id,
            metadata=request.metadata,
            resources=self._application_resources,
            asset_stores=self._asset_stores,
            custom_stores=self._custom_stores,
            plugin_bindings=(
                None if self._plugin_bindings is None else self._plugin_bindings()
            ),
        )

    def _credential_scope(self) -> Any:
        """Bind credentials separately from enumerable agent resources."""

        if self._credential_provider is None:
            return nullcontext()
        return _activate_credential_provider(self._credential_provider)

    def _session_scope(self, request: InvocationRequest) -> Any:
        """Bind one portable session lease around all agent lifecycle stages."""

        return invocation_session_context(
            self._session_store,
            framework=self.info.framework or "",
            user_id=request.user_id,
            session_id=request.session_id,
            invocation_id=request.invocation_id,
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

        with (
            activate_context(agent_context),
            self._credential_scope(),
            model_invocation_scope(lifecycle_context),
            tool_lifecycle_scope(self._extensions),
        ):
            try:
                event = await anext(iterator)
            except StopAsyncIteration:
                return _STREAM_END
            return await self._event(lifecycle_context, event)

    async def _short_stream(
        self,
        result: InvocationResult,
        lifecycle_context: LifecycleContext,
        agent_context: AgentContext,
        events: list[RuntimeEvent],
    ) -> AsyncIterator[RuntimeEvent]:
        """Project a lifecycle-finished response through normal stream policy."""

        for event in result.events:
            with activate_context(agent_context), self._credential_scope():
                transformed = await self._event(lifecycle_context, event)
            if transformed is DROP_EVENT:
                continue
            events.append(transformed)
            yield transformed
        with activate_context(agent_context), self._credential_scope():
            await self._after_stream(lifecycle_context, result)

    async def close(self) -> None:
        """Close framework execution before unwinding runtime resources."""

        async with self._resource_lock:
            if self._closed:
                return
            self._closed = True
            stack = self._resource_stack
            self._resource_stack = None
            self._application_resources = {}
            provider_started = self._credential_provider_started
            self._credential_provider_started = False
        failure = await _cleanup_failure(self._driver.close)
        if stack is not None:
            cleanup = await _cleanup_failure(stack.aclose)
            failure = _merge_cleanup_failure(
                failure, cleanup, label="runtime resource"
            )
        if provider_started and self._credential_provider is not None:
            cleanup = await _cleanup_failure(
                lambda: _close_credential_provider(self._credential_provider)
            )
            failure = _merge_cleanup_failure(
                failure, cleanup, label="credential provider"
            )
        if failure is not None:
            raise failure


async def _unwind_resource_start(
    stack: AsyncExitStack,
    provider: CredentialProvider | None,
    failure: BaseException,
) -> None:
    """Unwind partial extension startup without replacing its primary failure."""

    cleanup = await _cleanup_failure(stack.aclose)
    _merge_cleanup_failure(failure, cleanup, label="partial runtime resource")
    if provider is None:
        return
    cleanup = await _cleanup_failure(lambda: _close_credential_provider(provider))
    _merge_cleanup_failure(failure, cleanup, label="credential provider")


async def _start_wrapped_storage(driver: RuntimeDriver) -> None:
    """Start compiler-owned storage before listeners can consume its facades."""

    from .runtime_session import StorageRuntimeDriver

    if isinstance(driver, StorageRuntimeDriver):
        # The explicit type boundary avoids invoking arbitrary backend methods
        # that happen to use the same internal lifecycle name.
        await driver.start_owned_resources()


async def _cleanup_failure(callback: Any) -> BaseException | None:
    """Capture one cleanup failure so callers can retain deterministic priority."""

    try:
        await callback()
    except BaseException as failure:
        return failure
    return None


def _merge_cleanup_failure(
    primary: BaseException | None,
    cleanup: BaseException | None,
    *,
    label: str,
) -> BaseException | None:
    """Retain the first failure and annotate it with later cleanup types."""

    if cleanup is None:
        return primary
    if primary is None:
        return cleanup
    add_exception_note(
        primary, f"{label} cleanup also failed with {type(cleanup).__name__}"
    )
    return primary


async def _start_credential_provider(provider: CredentialProvider) -> None:
    """Start a provider once and clean partial initialization on failure."""

    failure = await _credential_provider_hook(provider, "start")
    if failure is None:
        return
    cleanup = await _credential_provider_hook(provider, "close")
    if cleanup is not None:
        add_exception_note(
            failure,
            "credential provider cleanup also failed with "
            f"{type(cleanup).__name__}"
        )
    raise failure


async def _close_credential_provider(provider: CredentialProvider) -> None:
    """Close a provider while keeping secret-bearing failures unreachable."""

    failure = await _credential_provider_hook(provider, "close")
    if failure is not None:
        raise failure


async def _credential_provider_hook(
    provider: CredentialProvider, hook: str
) -> BaseException | None:
    """Call a lifecycle hook and return only a detached sanitized failure."""

    try:
        await getattr(provider, hook)()
    except asyncio.CancelledError:
        return asyncio.CancelledError()
    except Exception as error:
        return CredentialProviderError(
            f"credential provider {hook} failed with {type(error).__name__}"
        )
    return None


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
