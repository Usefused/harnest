"""Portable explicit control flow around Harnest-owned HTTP applications."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
import inspect
from typing import Any

from .lifecycle import LifecycleListener
from .lifecycle_transition import Finish, Next, TransitionContext, UNCHANGED


_PHASES = frozenset({"before_http", "after_http", "on_http_error"})
_ASGIApp = Callable[[Mapping[str, Any], Any, Any], Awaitable[None]]


class HTTPLifecycleError(TypeError):
    """An HTTP interceptor returned invalid or unsafe control flow."""


@dataclass(frozen=True, slots=True)
class HTTPCallRequest:
    """The routing fields a server interceptor may inspect or replace."""

    method: str
    path: str

    def __post_init__(self) -> None:
        """Reject routing values that cannot be represented safely in ASGI."""

        if not isinstance(self.method, str) or not self.method.strip():
            raise ValueError("HTTP lifecycle method must be non-empty")
        if not isinstance(self.path, str) or not self.path.startswith("/"):
            raise ValueError("HTTP lifecycle path must be absolute")


@dataclass(frozen=True, slots=True)
class HTTPResponseHead:
    """A response status and opaque raw headers available before body emission."""

    status_code: int
    headers: tuple[tuple[bytes, bytes], ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        """Validate the response head without decoding secret-bearing headers."""

        if not isinstance(self.status_code, int) or isinstance(self.status_code, bool):
            raise TypeError("HTTP response status must be an integer")
        if not 100 <= self.status_code <= 599:
            raise ValueError("HTTP response status must be between 100 and 599")
        if any(not _raw_header(item) for item in self.headers):
            raise TypeError("HTTP response headers must contain byte pairs")


@dataclass(frozen=True, slots=True)
class HTTPLifecycleContext(TransitionContext):
    """Stable request identity without headers, cookies, bodies, or credentials."""

    transport: str
    method: str
    path: str
    user_id: str | None = None


class HTTPLifecycleMiddleware:
    """Apply portable HTTP interceptors without buffering streaming bodies."""

    def __init__(self, app: _ASGIApp, *, listeners: Sequence[LifecycleListener]) -> None:
        self._app = app
        self._listeners = tuple(
            sorted(
                (item for item in listeners if item.phase in _PHASES),
                key=_listener_order,
            )
        )

    async def __call__(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        """Intercept HTTP only; WebSocket frames retain their native streaming path."""

        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        context = _context(scope)
        request = HTTPCallRequest(context.method, context.path)
        try:
            transformed, finished = await self._before(context, request)
            if finished is not None:
                await _send_finished(finished, scope, receive, send)
                return
            updated_scope = _updated_scope(scope, transformed)
            guarded_send = _ResponseSend(self, context, updated_scope, receive, send)
            await self._app(updated_scope, receive, guarded_send)
        except BaseException as error:
            await self._notify(context, error)
            raise

    async def _before(
        self, context: HTTPLifecycleContext, request: HTTPCallRequest
    ) -> tuple[HTTPCallRequest, Finish[Any] | None]:
        """Run request interceptors with explicit continuation semantics."""

        current = request
        for listener in self._phase("before_http"):
            value = await _resolve(listener.callback(context, current))
            if isinstance(value, Finish):
                return current, value
            current = _next_request(listener, current, value)
        return current, None

    async def after(
        self, context: HTTPLifecycleContext, response: HTTPResponseHead
    ) -> tuple[HTTPResponseHead, Finish[Any] | None]:
        """Transform a response head before any bytes become visible."""

        current = response
        for listener in self._phase("after_http"):
            value = await _resolve(listener.callback(context, current))
            if isinstance(value, Finish):
                return current, value
            current = _next_response(listener, current, value)
        return current, None

    async def _notify(
        self, context: HTTPLifecycleContext, error: BaseException
    ) -> None:
        """Notify error observers without allowing them to mask the primary failure."""

        for listener in self._phase("on_http_error"):
            try:
                await _resolve(listener.callback(context, error))
            except BaseException:
                continue

    def _phase(self, phase: str) -> tuple[LifecycleListener, ...]:
        """Return one deterministic phase from the already bounded listener set."""

        return tuple(item for item in self._listeners if item.phase == phase)


@dataclass(slots=True)
class _ResponseSend:
    """Delay only the ASGI response head so streaming bodies stay streaming."""

    pipeline: HTTPLifecycleMiddleware
    context: HTTPLifecycleContext
    scope: Mapping[str, Any]
    receive: Any = field(repr=False)
    send: Any = field(repr=False)
    finished: bool = False

    async def __call__(self, message: Mapping[str, Any]) -> None:
        """Transform the first response head and pass later body chunks unchanged."""

        if self.finished:
            return
        if message.get("type") != "http.response.start":
            await self.send(message)
            return
        head = HTTPResponseHead(
            int(message["status"]), tuple(message.get("headers", ()))
        )
        transformed, finished = await self.pipeline.after(self.context, head)
        if finished is not None:
            self.finished = True
            await _send_finished(
                finished, self.scope, self.receive, self.send
            )
            return
        await self.send(
            {
                **dict(message),
                "status": transformed.status_code,
                "headers": list(transformed.headers),
            }
        )


def install_http_lifecycle(app: Any, listeners: Sequence[LifecycleListener]) -> None:
    """Install middleware only when authored HTTP interceptors were discovered."""

    selected = tuple(item for item in listeners if item.phase in _PHASES)
    if selected:
        app.add_middleware(HTTPLifecycleMiddleware, listeners=selected)


def _context(scope: Mapping[str, Any]) -> HTTPLifecycleContext:
    """Derive a payload-free context after authentication has populated state."""

    principal = scope.get("state", {}).get("harnest_principal")
    user_id = getattr(principal, "user_id", None)
    return HTTPLifecycleContext(
        transport="http",
        method=str(scope.get("method", "GET")).upper(),
        path=str(scope.get("path", "/")),
        user_id=user_id if isinstance(user_id, str) else None,
    )


def _updated_scope(
    scope: Mapping[str, Any], request: HTTPCallRequest
) -> Mapping[str, Any]:
    """Copy routing fields because ASGI scope ownership remains with the server."""

    updated = dict(scope)
    updated["method"] = request.method.upper()
    updated["path"] = request.path
    updated["raw_path"] = request.path.encode("utf-8")
    return updated


def _next_request(
    listener: LifecycleListener, current: HTTPCallRequest, value: Any
) -> HTTPCallRequest:
    """Require explicit request continuation for the new HTTP lifecycle."""

    if not isinstance(value, Next):
        raise _transition_error(listener, "context.next(...) or context.finish(...)")
    if value.value is UNCHANGED:
        return current
    if not isinstance(value.value, HTTPCallRequest):
        raise _transition_error(listener, "context.next(HTTPCallRequest)")
    return value.value


def _next_response(
    listener: LifecycleListener, current: HTTPResponseHead, value: Any
) -> HTTPResponseHead:
    """Require explicit response-head continuation before bytes are emitted."""

    if not isinstance(value, Next):
        raise _transition_error(listener, "context.next(...) or context.finish(...)")
    if value.value is UNCHANGED:
        return current
    if not isinstance(value.value, HTTPResponseHead):
        raise _transition_error(listener, "context.next(HTTPResponseHead)")
    return value.value


async def _send_finished(
    transition: Finish[Any],
    scope: Mapping[str, Any],
    receive: Any,
    send: Any,
) -> None:
    """Require a complete ASGI response when an HTTP interceptor stops execution."""

    response = transition.result
    if not callable(response):
        raise HTTPLifecycleError(
            "HTTP context.finish(...) requires an ASGI response"
        )
    result = response(scope, receive, send)
    if not inspect.isawaitable(result):
        raise HTTPLifecycleError("finished HTTP response must be asynchronous")
    await result


def _raw_header(value: Any) -> bool:
    """Validate one ASGI header without decoding or reflecting its contents."""

    return (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[0], bytes)
        and isinstance(value[1], bytes)
    )


def _transition_error(
    listener: LifecycleListener, expected: str
) -> HTTPLifecycleError:
    """Attribute invalid control flow to its stable source identity."""

    return HTTPLifecycleError(
        f"HTTP listener {listener.identity} must return {expected}"
    )


def _listener_order(listener: LifecycleListener) -> tuple[Any, ...]:
    """Use the compiler's deterministic source ordering at runtime too."""

    return (
        listener.order,
        listener.relative_path,
        listener.line,
        listener.function_name,
    )


async def _resolve(value: Any) -> Any:
    """Accept synchronous and asynchronous authored interceptors consistently."""

    return await value if inspect.isawaitable(value) else value


__all__ = [
    "HTTPCallRequest",
    "HTTPLifecycleContext",
    "HTTPLifecycleError",
    "HTTPLifecycleMiddleware",
    "HTTPResponseHead",
    "install_http_lifecycle",
]
