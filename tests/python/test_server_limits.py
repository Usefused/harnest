import asyncio
import unittest

from harnest.server_limits import RequestSizeLimitMiddleware


async def _consume_once(_scope, receive, _send):
    await receive()


class ServerLimitMiddlewareTests(unittest.TestCase):
    def test_rejects_chunked_http_body_without_content_length(self):
        received = [
            {"type": "http.request", "body": b"x" * 2048, "more_body": False}
        ]
        sent = []

        async def receive():
            return received.pop(0)

        async def send(message):
            sent.append(message)

        middleware = RequestSizeLimitMiddleware(
            _consume_once,
            max_request_bytes=1024,
        )
        asyncio.run(middleware({"type": "http", "headers": []}, receive, send))

        self.assertEqual(sent[0]["status"], 413)
        self.assertIn(b"Request body exceeds 1KiB", sent[1]["body"])

    def test_closes_oversized_websocket_frame(self):
        received = [{"type": "websocket.receive", "text": "x" * 2048}]
        sent = []

        async def receive():
            return received.pop(0)

        async def send(message):
            sent.append(message)

        middleware = RequestSizeLimitMiddleware(
            _consume_once,
            max_request_bytes=1024,
        )
        asyncio.run(middleware({"type": "websocket"}, receive, send))

        self.assertEqual(sent, [{"type": "websocket.close", "code": 1009}])

    def test_asset_upload_uses_fixed_binary_ceiling_only_on_collection_route(self):
        received = [
            {"type": "http.request", "body": b"x" * 2048, "more_body": False}
        ]
        sent = []

        async def receive():
            return received.pop(0)

        async def send(message):
            sent.append(message)

        middleware = RequestSizeLimitMiddleware(
            _consume_once,
            max_request_bytes=1024,
            max_asset_bytes=3072,
        )
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/sessions/demo/assets",
            "headers": [(b"content-length", b"2048")],
        }

        asyncio.run(middleware(scope, receive, send))

        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
