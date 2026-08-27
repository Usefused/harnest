"""ASGI request-size enforcement shared by neutral and native routes."""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Mapping

from .server_config import format_byte_size, validate_max_request_bytes


class _PayloadTooLarge(Exception):
    pass


class RequestSizeLimitMiddleware:
    """Reject oversized HTTP bodies and WebSocket frames before routing."""

    def __init__(self, app: Any, *, max_request_bytes: int) -> None:
        self._app = app
        self._max_bytes = validate_max_request_bytes(max_request_bytes)

    async def __call__(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        scope_type = scope.get("type")
        if scope_type == "http":
            await self._serve_http(scope, receive, send)
            return
        if scope_type == "websocket":
            await self._serve_websocket(scope, receive, send)
            return
        await self._app(scope, receive, send)

    async def _serve_http(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        if _content_length(scope) > self._max_bytes:
            await _reject_http(send, self._max_bytes)
            return
        limited_receive = _limited_http_receive(receive, self._max_bytes)
        try:
            await self._app(scope, limited_receive, send)
        except _PayloadTooLarge:
            await _reject_http(send, self._max_bytes)

    async def _serve_websocket(
        self, scope: Mapping[str, Any], receive: Any, send: Any
    ) -> None:
        limited_receive = _limited_websocket_receive(receive, self._max_bytes)
        try:
            await self._app(scope, limited_receive, send)
        except _PayloadTooLarge:
            await send({"type": "websocket.close", "code": 1009})


def install_request_size_limit(app: Any, max_request_bytes: int) -> None:
    app.add_middleware(
        RequestSizeLimitMiddleware,
        max_request_bytes=validate_max_request_bytes(max_request_bytes),
    )


def _content_length(scope: Mapping[str, Any]) -> int:
    headers = dict(scope.get("headers", ()))
    value = headers.get(b"content-length")
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _limited_http_receive(receive: Any, maximum: int) -> Callable[[], Awaitable[Any]]:
    size = 0

    async def limited() -> Any:
        nonlocal size
        message = await receive()
        if message.get("type") == "http.request":
            size += len(message.get("body", b""))
            if size > maximum:
                raise _PayloadTooLarge
        return message

    return limited


def _limited_websocket_receive(
    receive: Any, maximum: int
) -> Callable[[], Awaitable[Any]]:
    async def limited() -> Any:
        message = await receive()
        if message.get("type") != "websocket.receive":
            return message
        size = len(message.get("bytes") or b"")
        text = message.get("text")
        if isinstance(text, str):
            size += len(text.encode("utf-8"))
        if size > maximum:
            raise _PayloadTooLarge
        return message

    return limited


async def _reject_http(send: Any, maximum: int) -> None:
    body = json.dumps(
        {"detail": f"Request body exceeds {format_byte_size(maximum)}"},
        separators=(",", ":"),
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


__all__ = ["RequestSizeLimitMiddleware", "install_request_size_limit"]
