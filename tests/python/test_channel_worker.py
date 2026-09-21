"""Channel admission, durable work ownership, isolation, and ambiguous outcomes."""

import asyncio
from dataclasses import replace
import time
import unittest
from unittest.mock import AsyncMock

from harnest.channels import ChannelBinding, ChannelEvent, ChannelWorker
from harnest.task_store_memory import MemoryTaskStore
from harnest_fused import FusedChannelReceiver, slack_mention


class ChannelWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        """Share one store across replacement workers to model durable record ownership."""
        self.store = MemoryTaskStore()
        await self.store.start()
        self.addAsyncCleanup(self.store.close)
        self.invoke = AsyncMock(return_value="A test answer")
        self.send = AsyncMock(return_value={"ok": True})
        self.binding = ChannelBinding("slack", "fused", ("T1",), ("C1",))
        self.worker = self.make_worker()
        self.event = ChannelEvent("slack", "T1", "E1", "app_mention", "U1", "C1", "1.0", 1.0, content="hello")

    def make_worker(self, **changes):
        """Keep identity stable unless a test explicitly checks a different binding."""
        options = dict(application_id="agent1", binding_id="slack1", binding=self.binding,
                       store=self.store, invoke=self.invoke, send=self.send, allowed_senders=("U1", "U2"))
        options.update(changes)
        return ChannelWorker(**options)

    async def test_duplicate_admission_invokes_once_and_queues_threaded_reply(self):
        """Broker redelivery and concurrent workers cannot duplicate a claimed effect."""
        jobs = await asyncio.gather(*(self.worker.admit(replace(self.event, delivery_id=str(i))) for i in range(8)))
        self.assertEqual(len(set(jobs)), 1)
        await asyncio.gather(self.worker.step(), self.make_worker().step())
        await self.worker.step()
        self.invoke.assert_awaited_once()
        self.send.assert_awaited_once()
        self.assertEqual(self.send.call_args.args[0].reply_to, {"thread_id": "1.0"})
        await self.worker.admit(self.event)
        self.assertEqual(await self.worker.step(), 0)

    async def test_restart_recovers_pending_reply_without_invoking_again(self):
        """Persisted output remains sendable after the original worker disappears."""
        await self.worker.admit(self.event)
        await self.worker.step()
        self.send.assert_not_awaited()
        await self.make_worker().step()
        self.invoke.assert_awaited_once()
        self.send.assert_awaited_once()

    async def test_rejects_foreign_installation_conversation_sender_and_kind(self):
        """Allowlist failures never enqueue model work."""
        for change in ({"installation_id": "T2"}, {"conversation_id": "C2"},
                       {"sender_id": "U3"}, {"kind": "message_deleted"}, {"platform": "teams"}):
            self.assertIsNone(await self.worker.admit(replace(self.event, **change)))
        self.assertEqual(await self.worker.step(), 0)
        self.invoke.assert_not_awaited()

    async def test_policy_is_rechecked_before_sending_a_retained_reply(self):
        """A narrowed actor policy withdraws queued sends without modifying credentials."""
        await self.worker.admit(self.event)
        await self.worker.step()
        await self.make_worker(allowed_senders=("U2",)).step()
        self.send.assert_not_awaited()

    async def test_acceptance_watermark_ignores_older_retained_mentions(self):
        """A newly named receiver must not send unsolicited answers to historical events."""
        worker = self.make_worker(not_before=2)
        self.assertIsNone(await worker.admit(self.event))
        self.assertIsNotNone(await worker.admit(replace(self.event, occurred_at=3)))

    async def test_agent_binding_and_actor_isolate_jobs_and_sessions(self):
        """A shared provider bucket never becomes a shared Harnest conversation."""
        other = self.make_worker(application_id="agent2")
        await self.worker.admit(self.event)
        await other.admit(self.event)
        await self.worker.admit(replace(self.event, provider_event_id="E2", sender_id="U2", thread_id="1.0"))
        await self.worker.step()
        await self.worker.step()
        await other.step()
        self.assertEqual(self.invoke.await_count, 3)
        self.assertEqual(len({call.args[1] for call in self.invoke.call_args_list}), 3)
        self.assertEqual(len({call.args[2] for call in self.invoke.call_args_list}), 3)

    async def test_ambiguous_send_is_retained_as_failed_and_never_blindly_retried(self):
        """A timeout may follow provider acceptance, so automatic replay is unsafe."""
        identity = await self.worker.admit(self.event)
        await self.worker.step()
        self.send.side_effect = TimeoutError("private provider details")
        await self.worker.step()
        await self.make_worker().step()
        self.send.assert_awaited_once()
        record = await self.store.get_task(application_id=self.worker.scope, job_id="reply:" + identity)
        self.assertEqual((record.status, record.failure_code), ("failed", "delivery_unknown"))
        self.assertNotIn("private", repr(record))

    async def test_crashed_claim_is_not_replayed_as_another_agent_turn(self):
        """Pending work survives, while ambiguous in-flight work becomes an operator failure."""
        identity = await self.worker.admit(self.event)
        await self.store.claim_tasks(application_id=self.worker.scope, queues=("channel.inbox",),
                                     now=time.time() - 20, lease_seconds=1)
        await self.make_worker().step()
        self.invoke.assert_not_awaited()
        record = await self.store.get_task(application_id=self.worker.scope, job_id=identity)
        self.assertEqual(record.status, "failed")

    async def test_cancelled_owner_cannot_queue_a_reply_after_invocation_returns(self):
        """Fence delivery intent when cancellation withdraws the running job's lease."""
        identity = await self.worker.admit(self.event)
        record = await self.store.get_task(application_id=self.worker.scope, job_id=identity)

        async def cancelled(*_arguments):
            """Simulate cancellation while the agent's effect is already in progress."""
            await self.store.cancel_task(application_id=self.worker.scope, job_id=identity,
                                         user_id=record.user_id, now=time.time())
            return "late response"

        self.invoke.side_effect = cancelled
        await self.worker.step()
        self.assertEqual(await self.worker.step(), 0)
        self.send.assert_not_awaited()

    async def test_receiver_ack_is_after_admission_and_storage_failure_nacks(self):
        """ACK proves durable responsibility, never merely parsing or model completion."""
        context = {"ack": AsyncMock(), "nack": AsyncMock()}
        receiver = FusedChannelReceiver(None, self.worker, lambda _: self.event)
        await receiver.handle({}, context)
        context["ack"].assert_awaited_once()
        self.invoke.assert_not_awaited()
        self.worker.admit = AsyncMock(side_effect=OSError("database unavailable"))
        with self.assertRaisesRegex(RuntimeError, "admission failed"):
            await receiver.handle({}, context)
        context["nack"].assert_awaited_once()
        context["ack"].assert_awaited_once()

    def test_slack_mapping_checks_actual_app_team_and_bot_fields(self):
        """Installation identity comes from the verified envelope, never a constant overwrite."""
        payload = {"body": {"team_id": "T1", "api_app_id": "A1", "event_id": "E1", "event": {
            "type": "app_mention", "user": "U1", "channel": "C1", "ts": "1.0", "text": "hi"}}}
        self.assertEqual(slack_mention(payload, installation_id="T1", app_id="A1").provider_event_id, "E1")
        self.assertIsNone(slack_mention(payload, installation_id="T2", app_id="A1"))
        self.assertIsNone(slack_mention(payload, installation_id="T1", app_id="A2"))
        payload["body"]["event"]["bot_id"] = "B1"
        self.assertIsNone(slack_mention(payload, installation_id="T1", app_id="A1"))
