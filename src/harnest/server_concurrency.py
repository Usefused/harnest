"""ASGI in-flight request cap for the standalone Harnest HTTP server."""

from __future__ import annotations

from typing import Any, Callable, Mapping

_Scope = Mapping[str, Any]
_REJECTION_BODY = b"Service Unavailable"
# 1013 ("Try Again Later") is the WebSocket analogue of HTTP 503.
_TRY_AGAIN_LATER = 1013


def _never_exempt(scope: _Scope) -> bool:
    """Default policy: every HTTP and WebSocket request counts toward the cap."""

    del scope
    return False


class ConcurrencyLimitMiddleware:
    """Reject requests beyond ``http.maxConcurrentRequests`` with HTTP 503.

    This replaces uvicorn's ``limit_concurrency``, which counts open
    connections (including idle keep-alive sockets) rather than in-flight
    requests. That made a browser's parallel asset fetches trip the limit long
    before any agent work was running.
    """

    def __init__(
        self,
        app: Any,
        *,
        max_concurrent_requests: int,
        exempt: Callable[[_Scope], bool] = _never_exempt,
    ) -> None:
        """Validate the cap once so a misconfiguration fails at startup."""

        if (
            isinstance(max_concurrent_requests, bool)
            or not isinstance(max_concurrent_requests, int)
            or max_concurrent_requests < 1
        ):
            raise ValueError("max concurrent requests must be a positive integer")
        self._app = app
        self._limit = max_concurrent_requests
        self._exempt = exempt
        self._active = 0

    async def __call__(self, scope: _Scope, receive: Any, send: Any) -> None:
        """Hold one slot for the full request, including streamed responses."""

        if scope.get("type") not in {"http", "websocket"} or self._exempt(scope):
            await self._app(scope, receive, send)
            return
        # The event loop is single-threaded and there is no await between the
        # check and the increment, so admission cannot race past the cap.
        if self._active >= self._limit:
            await _reject(scope, send)
            return
        self._active += 1
        try:
            await self._app(scope, receive, send)
        finally:
            self._active -= 1


def playground_page_request(scope: _Scope) -> bool:
    """Identify Playground page and asset reads, which never invoke the agent.

    A single Playground load fetches the page plus roughly ten scripts and
    stylesheets in parallel. Counting them against the agent's request cap
    returned 503s for core UI files, leaving the Playground and Evals views
    inert. Writes under ``/_harnest/`` (such as eval runs) still count.
    """

    if scope.get("type") != "http" or scope.get("method") not in {"GET", "HEAD"}:
        return False
    path = str(scope.get("path", ""))
    return path == "/" or path.startswith("/_harnest/")


def install_concurrency_limit(
    app: Any, max_concurrent_requests: int, *, playground_enabled: bool
) -> None:
    """Install the request cap outermost so rejection happens before routing."""

    exempt = playground_page_request if playground_enabled else _never_exempt
    app.add_middleware(
        ConcurrencyLimitMiddleware,
        max_concurrent_requests=max_concurrent_requests,
        exempt=exempt,
    )


async def _reject(scope: _Scope, send: Any) -> None:
    """Match uvicorn's previous plain-text 503 so existing clients see no change."""

    if scope.get("type") == "websocket":
        await _reject_websocket(scope, send)
        return
    await send(
        {
            "type": "http.response.start",
            "status": 503,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(_REJECTION_BODY)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _REJECTION_BODY})


async def _reject_websocket(scope: _Scope, send: Any) -> None:
    """Prefer an HTTP 503 handshake response when the server supports it."""

    extensions = scope.get("extensions") or {}
    if "websocket.http.response" not in extensions:
        await send({"type": "websocket.close", "code": _TRY_AGAIN_LATER})
        return
    await send(
        {
            "type": "websocket.http.response.start",
            "status": 503,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        }
    )
    await send({"type": "websocket.http.response.body", "body": _REJECTION_BODY})


__all__ = [
    "ConcurrencyLimitMiddleware",
    "install_concurrency_limit",
    "playground_page_request",
]
