"""Per-model LiteLLM request lifecycle without process-global mutation."""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal, TypeAlias

LifecycleValue: TypeAlias = Any | Awaitable[Any]


@dataclass(frozen=True, slots=True)
class LiteLLMContext:
    """Stable call context provided to every LiteLLM lifecycle hook."""

    model: str
    framework: Literal["adk", "langgraph"]
    transport: Any | None = None


class LiteLLMLifecycle:
    """Customize one LiteLLM model's transport and call lifecycle.

    Override hooks with either ordinary or ``async`` methods. Async hooks require
    the framework's async invocation path; Harnest never runs a private event
    loop to make them appear synchronous.
    """

    def create_transport(self, context: LiteLLMContext) -> LifecycleValue:
        """Return a provider client accepted by LiteLLM's ``client`` option."""

        return None

    def before_request(
        self, request: dict[str, Any], context: LiteLLMContext
    ) -> LifecycleValue:
        """Mutate ``request`` or return its replacement before LiteLLM runs."""

        return request

    def after_response(self, response: Any, context: LiteLLMContext) -> LifecycleValue:
        """Return a non-stream response replacement or observe stream exhaustion."""

        return response

    def on_error(self, error: BaseException, context: LiteLLMContext) -> LifecycleValue:
        """Observe a lifecycle or LiteLLM failure before it is re-raised."""

        return None

    def close(self, context: LiteLLMContext) -> LifecycleValue:
        """Release resources created for this model instance."""

        return None


class _LifecycleLiteLLMClient:
    """Apply lifecycle hooks around one adapter's LiteLLM client."""

    def __init__(
        self,
        delegate: Any,
        lifecycle: LiteLLMLifecycle,
        *,
        model: str,
        framework: Literal["adk", "langgraph"],
    ) -> None:
        self._delegate = delegate
        self._lifecycle = lifecycle
        self._context = LiteLLMContext(model=model, framework=framework)
        self._transport: Any | None = None
        self._initialized = False
        self._state: Literal["open", "closing", "closed"] = "open"
        self._active_calls = 0
        self._mode: Literal["sync", "async"] | None = None
        self._state_changed = threading.Condition()
        self._sync_init_lock = threading.Lock()
        self._async_init_lock = asyncio.Lock()

    async def acompletion(self, **kwargs: Any) -> Any:
        """Run one asynchronous LiteLLM completion with lifecycle hooks."""

        self._begin_call("async")
        stream_owned = False
        try:
            await self._ensure_transport_async()
            request = await self._before_async(kwargs)
            response = await self._delegate.acompletion(**request)
            if _is_async_stream(response):
                stream_owned = True
                return _AsyncLifecycleStream(response, self)
            return await _await_value(
                self._lifecycle.after_response(response, self._context)
            )
        except BaseException as error:
            await self._notify_error_async(error)
            raise
        finally:
            if not stream_owned:
                self._end_call()

    def completion(self, **kwargs: Any) -> Any:
        """Run with synchronous hooks or reject async hooks explicitly."""

        self._begin_call("sync")
        stream_owned = False
        try:
            self._ensure_transport_sync()
            request = self._before_sync(kwargs)
            response = self._delegate.completion(**request)
            if _is_sync_stream(response):
                stream_owned = True
                return _SyncLifecycleStream(response, self)
            return _sync_value(
                self._lifecycle.after_response(response, self._context),
                hook="after_response",
            )
        except BaseException as error:
            self._notify_error_sync(error)
            raise
        finally:
            if not stream_owned:
                self._end_call()

    async def aclose(self) -> None:
        """Close the lifecycle once, using its asynchronous contract."""

        if not await self._begin_close_async():
            return
        try:
            await _await_value(self._lifecycle.close(self._context))
        except BaseException:
            self._finish_close(success=False)
            raise
        self._finish_close(success=True)

    def close(self) -> None:
        """Close synchronously only when the hook is synchronous."""

        if not self._begin_close():
            return
        try:
            _sync_value(self._lifecycle.close(self._context), hook="close")
        except BaseException:
            self._finish_close(success=False)
            raise
        self._finish_close(success=True)

    def _begin_call(self, mode: Literal["sync", "async"]) -> None:
        with self._state_changed:
            if self._state != "open":
                raise RuntimeError("LiteLLM lifecycle is closed")
            if self._mode is None:
                self._mode = mode
            elif self._mode != mode:
                raise RuntimeError(
                    "a lifecycle-enabled LiteLLM model cannot mix synchronous "
                    "and asynchronous execution"
                )
            self._active_calls += 1

    def _end_call(self) -> None:
        with self._state_changed:
            self._active_calls -= 1
            if self._active_calls == 0:
                self._state_changed.notify_all()

    async def _ensure_transport_async(self) -> None:
        if self._initialized:
            return
        async with self._async_init_lock:
            if self._initialized:
                return
            transport = await _await_value(
                self._lifecycle.create_transport(self._context)
            )
            self._set_transport(transport)

    def _ensure_transport_sync(self) -> None:
        if self._initialized:
            return
        with self._sync_init_lock:
            if self._initialized:
                return
            transport = _sync_value(
                self._lifecycle.create_transport(self._context),
                hook="create_transport",
            )
            self._set_transport(transport)

    def _set_transport(self, transport: Any) -> None:
        self._transport = transport
        self._context = replace(self._context, transport=transport)
        self._initialized = True

    async def _before_async(self, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        request = self._request(kwargs)
        replacement = await _await_value(
            self._lifecycle.before_request(request, self._context)
        )
        return self._validated_request(request, replacement)

    def _before_sync(self, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        request = self._request(kwargs)
        replacement = _sync_value(
            self._lifecycle.before_request(request, self._context),
            hook="before_request",
        )
        return self._validated_request(request, replacement)

    def _request(self, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        request = dict(kwargs)
        if self._transport is not None:
            request["client"] = self._transport
        return request

    def _validated_request(
        self, original: dict[str, Any], replacement: Any
    ) -> dict[str, Any]:
        value = original if replacement is None else replacement
        if not isinstance(value, Mapping):
            raise TypeError("LiteLLM before_request must return a mapping or None")
        request = dict(value)
        client = request.get("client")
        if client is not None and client is not self._transport:
            # The transport is instance-owned; allowing hooks to swap it would
            # make cleanup and isolation dependent on call order.
            raise ValueError("LiteLLM before_request cannot replace lifecycle transport")
        return request

    def _begin_close(self) -> bool:
        with self._state_changed:
            while self._state == "closing":
                self._state_changed.wait()
            if self._state == "closed":
                return False
            self._state = "closing"
            while self._active_calls:
                self._state_changed.wait()
            return True

    async def _begin_close_async(self) -> bool:
        waiter = asyncio.create_task(asyncio.to_thread(self._begin_close))
        try:
            return await asyncio.shield(waiter)
        except asyncio.CancelledError:
            owns_close = await waiter
            if owns_close:
                self._finish_close(success=False)
            raise

    def _finish_close(self, *, success: bool) -> None:
        with self._state_changed:
            self._state = "closed" if success else "open"
            self._state_changed.notify_all()

    async def _after_stream_async(self, response: Any) -> None:
        await _await_value(self._lifecycle.after_response(response, self._context))

    def _after_stream_sync(self, response: Any) -> None:
        _sync_value(
            self._lifecycle.after_response(response, self._context),
            hook="after_response",
        )

    async def _notify_error_async(self, error: BaseException) -> None:
        try:
            await _await_value(self._lifecycle.on_error(error, self._context))
        except BaseException as observer_error:
            _note_observer_error(error, observer_error)

    def _notify_error_sync(self, error: BaseException) -> None:
        try:
            _sync_value(
                self._lifecycle.on_error(error, self._context), hook="on_error"
            )
        except BaseException as observer_error:
            _note_observer_error(error, observer_error)


class _AsyncLifecycleStream:
    """Keep one async model call active until its stream terminates."""

    def __init__(self, source: Any, owner: _LifecycleLiteLLMClient) -> None:
        self._source = source
        self._iterator = source.__aiter__()
        self._owner = owner
        self._done = False

    def __aiter__(self) -> "_AsyncLifecycleStream":
        return self

    async def __anext__(self) -> Any:
        if self._done:
            raise StopAsyncIteration
        try:
            return await self._iterator.__anext__()
        except StopAsyncIteration:
            await self._finish_success()
            raise
        except BaseException as error:
            await self._finish_error(error)
            raise

    async def aclose(self) -> None:
        if self._done:
            return
        try:
            closer = getattr(self._iterator, "aclose", None)
            if callable(closer):
                await closer()
        except BaseException as error:
            await self._owner._notify_error_async(error)
            raise
        finally:
            self._release()

    async def _finish_success(self) -> None:
        try:
            await self._owner._after_stream_async(self._source)
        except BaseException as error:
            await self._owner._notify_error_async(error)
            raise
        finally:
            self._release()

    async def _finish_error(self, error: BaseException) -> None:
        try:
            await self._owner._notify_error_async(error)
        finally:
            self._release()

    def _release(self) -> None:
        if not self._done:
            self._done = True
            self._owner._end_call()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._source, name)


class _SyncLifecycleStream:
    """Keep one synchronous model call active until its stream terminates."""

    def __init__(self, source: Any, owner: _LifecycleLiteLLMClient) -> None:
        self._source = source
        self._iterator = iter(source)
        self._owner = owner
        self._done = False

    def __iter__(self) -> "_SyncLifecycleStream":
        return self

    def __next__(self) -> Any:
        if self._done:
            raise StopIteration
        try:
            return next(self._iterator)
        except StopIteration:
            self._finish_success()
            raise
        except BaseException as error:
            self._finish_error(error)
            raise

    def close(self) -> None:
        if self._done:
            return
        try:
            closer = getattr(self._iterator, "close", None)
            if callable(closer):
                closer()
        except BaseException as error:
            self._owner._notify_error_sync(error)
            raise
        finally:
            self._release()

    def _finish_success(self) -> None:
        try:
            self._owner._after_stream_sync(self._source)
        except BaseException as error:
            self._owner._notify_error_sync(error)
            raise
        finally:
            self._release()

    def _finish_error(self, error: BaseException) -> None:
        try:
            self._owner._notify_error_sync(error)
        finally:
            self._release()

    def _release(self) -> None:
        if not self._done:
            self._done = True
            self._owner._end_call()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._source, name)


def _is_async_stream(value: Any) -> bool:
    return callable(getattr(value, "__aiter__", None))


def _is_sync_stream(value: Any) -> bool:
    return callable(getattr(value, "__next__", None)) and callable(
        getattr(value, "__iter__", None)
    )


async def _await_value(value: LifecycleValue) -> Any:
    return await value if inspect.isawaitable(value) else value


def _sync_value(value: LifecycleValue, *, hook: str) -> Any:
    if not inspect.isawaitable(value):
        return value
    # Close coroutine objects so rejecting an async hook never emits an
    # un-awaited-coroutine warning or runs it on a hidden event loop.
    closer = getattr(value, "close", None)
    if callable(closer):
        closer()
    raise RuntimeError(
        f"LiteLLM lifecycle hook {hook} is async; use asynchronous model execution"
    )


def _note_observer_error(error: BaseException, observer_error: BaseException) -> None:
    # Error observation is diagnostic and must not replace the model failure
    # that callers need to classify or retry.
    add_note = getattr(error, "add_note", None)
    if callable(add_note):
        add_note(f"LiteLLM on_error hook failed: {observer_error}")


def _lifecycle_resources(value: Any) -> tuple[Any, ...]:
    resources = getattr(value, "__harnest_litellm_resources__", ())
    return tuple(resources) if isinstance(resources, (tuple, list)) else ()


def _attach_lifecycle_resource(value: Any, resource: Any) -> Any:
    """Register one cleanup owner once despite repeated wrapper propagation."""

    current = _lifecycle_resources(value)
    if not any(item is resource for item in current):
        # The same adapter can reach a target through several graph paths;
        # identity deduplication prevents duplicate cleanup responsibilities.
        object.__setattr__(value, "__harnest_litellm_resources__", (*current, resource))
    return value


def create_adk_lifecycle_client(
    client_type: type[Any], lifecycle: LiteLLMLifecycle, *, model: str
) -> Any:
    """Create the concrete client type required by ADK's validated adapter."""

    delegate = client_type()
    controller = _LifecycleLiteLLMClient(
        delegate, lifecycle, model=model, framework="adk"
    )

    class ADKLifecycleClient(client_type):
        async def acompletion(
            self, model: Any, messages: Any, tools: Any, **kwargs: Any
        ) -> Any:
            return await controller.acompletion(
                model=model, messages=messages, tools=tools, **kwargs
            )

        def completion(
            self,
            model: Any,
            messages: Any,
            tools: Any,
            stream: bool = False,
            **kwargs: Any,
        ) -> Any:
            return controller.completion(
                model=model,
                messages=messages,
                tools=tools,
                stream=stream,
                **kwargs,
            )

        async def aclose(self) -> None:
            await controller.aclose()

        def close(self) -> None:
            controller.close()

    client = ADKLifecycleClient()
    client.__harnest_controller__ = controller
    return client


def propagate_litellm_lifecycles(source: Any, target: Any) -> Any:
    """Propagate cleanup resources and borrowed transport metadata together."""

    from .model_transport import propagate_model_transport_bindings

    for resource in _lifecycle_resources(source):
        _attach_lifecycle_resource(target, resource)
    return propagate_model_transport_bindings(source, target)


async def close_litellm_lifecycles(value: Any) -> None:
    """Close lifecycle resources attached to an adapter or compiled target."""

    failures: list[BaseException] = []
    for resource in reversed(_lifecycle_resources(value)):
        try:
            await resource.aclose()
        except BaseException as error:
            failures.append(error)
    if failures:
        _raise_cleanup_failures(failures)


def _raise_cleanup_failures(failures: list[BaseException]) -> None:
    cancellation = next(
        (error for error in failures if isinstance(error, asyncio.CancelledError)),
        None,
    )
    primary = cancellation or failures[0]
    add_note = getattr(primary, "add_note", None)
    if callable(add_note):
        for error in failures:
            if error is not primary:
                add_note(
                    "additional LiteLLM lifecycle cleanup failure: "
                    f"{type(error).__name__}"
                )
    raise primary


__all__ = ["LiteLLMContext", "LiteLLMLifecycle"]
