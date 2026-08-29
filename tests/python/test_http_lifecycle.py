from __future__ import annotations

from typing import Any
import unittest

from starlette.responses import PlainTextResponse
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harnest.http_lifecycle import (
    HTTPCallRequest,
    HTTPLifecycleMiddleware,
    HTTPResponseHead,
    install_http_lifecycle,
)
from harnest.lifecycle import LifecycleListener
from harnest.runtime_auth import AuthPrincipal, install_authentication


def _listener(phase: str, callback: Any, *, order: int = 0) -> LifecycleListener:
    return LifecycleListener(phase, callback, order, "http.py", 1, phase)


async def _request(
    middleware: HTTPLifecycleMiddleware,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    received: list[dict[str, Any]] = []
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        message = {"type": "http.request", "body": b"", "more_body": False}
        received.append(message)
        return message

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/original",
        "raw_path": b"/original",
        "headers": [],
        "state": {},
    }
    await middleware(scope, receive, send)
    return received, sent


class HTTPLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_lifecycle_replaces_route_and_response_head(self) -> None:
        calls: list[str] = []

        async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            del receive
            calls.append(scope["path"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        def before(context: Any, request: HTTPCallRequest):
            return context.next(HTTPCallRequest(request.method, "/rewritten"))

        def after(context: Any, response: HTTPResponseHead):
            return context.next(HTTPResponseHead(202, response.headers))

        middleware = HTTPLifecycleMiddleware(
            app,
            listeners=(
                _listener("before_http", before),
                _listener("after_http", after),
            ),
        )

        _, sent = await _request(middleware)

        self.assertEqual(calls, ["/rewritten"])
        self.assertEqual(sent[0]["status"], 202)
        self.assertEqual(sent[1]["body"], b"ok")

    async def test_http_lifecycle_can_finish_before_routing(self) -> None:
        called = False

        async def app(scope: Any, receive: Any, send: Any) -> None:
            nonlocal called
            called = True

        def before(context: Any, request: HTTPCallRequest):
            del request
            return context.finish(PlainTextResponse("blocked", status_code=403))

        middleware = HTTPLifecycleMiddleware(
            app, listeners=(_listener("before_http", before),)
        )

        _, sent = await _request(middleware)

        self.assertFalse(called)
        self.assertEqual(sent[0]["status"], 403)
        self.assertEqual(sent[1]["body"], b"blocked")

    async def test_http_lifecycle_requires_explicit_next(self) -> None:
        async def app(scope: Any, receive: Any, send: Any) -> None:
            raise AssertionError("invalid hook must stop before routing")

        middleware = HTTPLifecycleMiddleware(
            app,
            listeners=(_listener("before_http", lambda context, request: None),),
        )

        with self.assertRaisesRegex(TypeError, "context.next"):
            await _request(middleware)


class HTTPLifecycleIntegrationTests(unittest.TestCase):
    def test_authenticated_identity_reaches_http_lifecycle_without_headers(self) -> None:
        """Keep authentication outside lifecycle middleware at the server boundary."""

        observed: list[tuple[str | None, bool]] = []

        class Authenticator:
            async def authenticate(self, connection: Any) -> AuthPrincipal:
                return AuthPrincipal(connection.headers["x-user"])

        def before(context: Any, request: HTTPCallRequest):
            observed.append((context.user_id, hasattr(context, "headers")))
            return context.next()

        app = FastAPI()

        @app.get("/private")
        async def private() -> dict[str, bool]:
            return {"ok": True}

        install_http_lifecycle(
            app, (_listener("before_http", before),)
        )
        install_authentication(app, Authenticator())

        with TestClient(app) as client:
            response = client.get("/private", headers={"x-user": "user-1"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(observed, [("user-1", False)])
