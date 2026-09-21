"""Real Harnest HTTP governance and Fused reply execution boundary tests."""

import tempfile
from pathlib import Path
import unittest

import httpx

from harnest.bundle import compile_artifact
from harnest.channels import ChannelHTTPInvoker, ChannelEvent, ChannelReply
from harnest.runtime import create_fastapi_app
from harnest_fused import FusedSlackReplySender
from _session_store_fixture import write_session_store
from _http_compiled_channel_fixture import write_agent


class ChannelHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_compiled_backends_authenticate_actor_isolate_session_and_reject_approval(self):
        """Invoke real compiled ADK/LangGraph through their normal HTTP coordinator."""
        for framework in ("adk", "langgraph"):
            with self.subTest(framework=framework), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "source"
                write_agent(root)
                write_session_store(root)
                artifact = Path(temporary) / "compiled"
                compile_artifact(root, artifact, framework=framework)
                app = create_fastapi_app(artifact)
                async with app.router.lifespan_context(app), httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://agent.test",
                ) as client:
                    invoke = ChannelHTTPInvoker(client, lambda actor: {"Authorization": "Bearer test-worker", "X-Channel-Actor": actor})
                    event = ChannelEvent("slack", "T1", "E1", "app_mention", "U1", "C1", "1", 1, content="hello")
                    self.assertEqual(await invoke(event, "session1", "actor1"), "hello")
                    self.assertEqual(await invoke(event, "session1", "actor1"), "hello")
                    hidden = await client.get("/sessions/session1", headers={"Authorization": "Bearer test-worker", "X-Channel-Actor": "actor2"})
                    self.assertEqual(hidden.status_code, 404)
                    anonymous = ChannelHTTPInvoker(client, lambda _: {})
                    with self.assertRaises(httpx.HTTPStatusError):
                        await anonymous(event, "session2", "actor1")
                    from dataclasses import replace
                    with self.assertRaisesRegex(RuntimeError, "operator attention"):
                        await invoke(replace(event, content="approval"), "session3", "actor1")

    async def test_fused_reply_checks_provider_body_and_uses_persisted_thread(self):
        """A provider error inside HTTP 200 must never be recorded as sent."""
        import json
        requests = []
        result = {"ok": True, "channel": "C1", "ts": "2"}

        def transport(request):
            """Capture Engine API input while varying the provider outcome."""
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"status_code": 200, "results": [dict(result)]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport), base_url="http://engine.test") as client:
            send = FusedSlackReplySender(client, app_id="00000000-0000-0000-0000-000000000001",
                                        operation="chat.postMessage", selector={"end_user_ref": "approved-bot"})
            reply = ChannelReply("r1", "slack", "T1", "C1", {"thread_id": "1"}, "hello")
            self.assertEqual(await send(reply), {"message_id": "2"})
            self.assertEqual(requests[0]["input"]["thread_ts"], "1")
            self.assertFalse(requests[0]["input"]["reply_broadcast"])
            self.assertEqual(requests[0]["selector"], {"end_user_ref": "approved-bot"})
            result["ok"] = False
            with self.assertRaisesRegex(RuntimeError, "did not confirm"):
                await send(reply)
