"""Run channel ownership and restart cases against real PostgreSQL transactions."""

import os
import unittest
import uuid

from harnest.task_store_postgres import PostgresTaskStore
import test_channel_worker as contract


_DSN = os.environ.get("HARNEST_TEST_POSTGRES_DSN")


@unittest.skipUnless(_DSN, "set HARNEST_TEST_POSTGRES_DSN for durable channel worker integration")
class PostgresChannelWorkerTests(contract.ChannelWorkerTests):
    async def asyncSetUp(self):
        """Use a fresh test scope and register cleanup before closing the shared pool."""
        self.prefix, self.scopes = uuid.uuid4().hex, set()
        await super().asyncSetUp()
        self.store = PostgresTaskStore(_DSN)
        await self.store.start()
        self.addAsyncCleanup(self.store.close)
        self.addAsyncCleanup(self.clear_scope)
        self.worker = self.make_worker()

    def make_worker(self, **changes):
        """Track only this test's application identities for bounded record cleanup."""
        changes["application_id"] = self.prefix + ":" + changes.get("application_id", "agent1")
        worker = super().make_worker(**changes)
        self.scopes.add(worker.scope)
        return worker

    async def clear_scope(self):
        """Remove only this case's tasks without disturbing other channel jobs."""
        async with self.store._connection() as connection:
            await connection.execute("DELETE FROM harnest_durable_tasks WHERE application_id=ANY($1::text[])", list(self.scopes))

    async def test_fresh_pool_recovers_admitted_input_and_unsent_reply(self):
        """Close all connections at both crash boundaries and recover through a new pool."""
        await self.worker.admit(self.event)
        await self.store.close()
        await self.store.start()
        await self.make_worker().step()
        self.invoke.assert_awaited_once()
        self.send.assert_not_awaited()
        await self.store.close()
        await self.store.start()
        await self.make_worker().step()
        self.send.assert_awaited_once()
        self.invoke.assert_awaited_once()

    async def test_sdk_admission_through_compiled_agent_to_threaded_engine_reply(self):
        """Exercise the complete bridge with PostgreSQL and the real Harnest HTTP runtime."""
        import json
        from functools import partial
        from pathlib import Path
        import tempfile
        from unittest.mock import AsyncMock
        import httpx
        from harnest.bundle import compile_artifact
        from harnest.channels import ChannelHTTPInvoker
        from harnest.runtime import create_fastapi_app
        from harnest_fused import FusedChannelReceiver, FusedSlackReplySender, slack_mention
        from _http_compiled_channel_fixture import write_agent
        from _session_store_fixture import write_session_store

        sent = []

        def engine(request):
            """Model only Fused's execution boundary; preserve the real reply request contract."""
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"status_code": 200, "results": [{"ok": True, "channel": "C1", "ts": "2"}]})

        with tempfile.TemporaryDirectory() as directory:
            source, artifact = Path(directory) / "source", Path(directory) / "artifact"
            write_agent(source)
            write_session_store(source)
            compile_artifact(source, artifact, framework="adk")
            app = create_fastapi_app(artifact)
            async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://agent.test",
            ) as agent, httpx.AsyncClient(transport=httpx.MockTransport(engine), base_url="http://engine.test") as client:
                invoke = ChannelHTTPInvoker(agent, lambda actor: {"Authorization": "Bearer test-worker", "X-Channel-Actor": actor})
                send = FusedSlackReplySender(client, app_id="00000000-0000-0000-0000-000000000001",
                                            operation="chat.postMessage", selector={"end_user_ref": "approved-bot"})
                worker = self.make_worker(invoke=invoke, send=send)
                receiver = FusedChannelReceiver(None, worker, partial(slack_mention, installation_id="T1", app_id="A1"))
                context = {"ack": AsyncMock(), "nack": AsyncMock()}
                event = {"body": {"team_id": "T1", "api_app_id": "A1", "event_id": "E1", "event": {
                    "type": "app_mention", "channel": "C1", "user": "U1", "ts": "1.0", "text": "test channel"}}}
                await receiver.handle(event, context)
                context["ack"].assert_awaited_once()
                self.assertEqual(sent, [])
                await self.store.close()
                await self.store.start()
                await worker.step()
                await worker.step()
                await receiver.handle(event, context)
                await worker.step()
                self.assertEqual(len(sent), 1)
                self.assertEqual(sent[0]["input"]["text"], "test channel")
                self.assertEqual(sent[0]["input"]["thread_ts"], "1.0")
                context["nack"].assert_not_awaited()
