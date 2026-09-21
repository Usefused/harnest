"""Durable channel intake and delivery using Harnest's existing task store."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
import hashlib
import json
import math
import time
from typing import Any, TYPE_CHECKING

from .channel_storage import ChannelEvent, ChannelReply
if TYPE_CHECKING:
    from .channels import ChannelBinding
from .task_storage import TaskRecord, TaskStore


def channel_identity(*parts: str) -> str:
    """Hash a structured scope without ambiguous separators or exposed actor IDs."""
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


class ChannelWorker:
    """Admit before ACK, fence work, and persist replies before any provider send.

    The caller owns the started task store and authenticated invoke/send callbacks.
    Jobs never automatically retry an ambiguous model execution or provider send.
    Pending jobs survive restart; interrupted claimed jobs require reconciliation.
    """

    def __init__(self, *, application_id: str, binding_id: str, binding: ChannelBinding,
                 store: TaskStore, invoke: Any, send: Any, allowed_senders: tuple[str, ...],
                 allowed_kinds: tuple[str, ...] = ("app_mention",), timeout: float = 120,
                 not_before: float = 0) -> None:
        """Require explicit routing and actor policy before subscribing to events."""
        if not application_id or not binding_id or not allowed_senders:
            raise ValueError("application_id, binding_id, and allowed_senders are required")
        if not binding.allowed_installations or not binding.allowed_conversations or not allowed_kinds:
            raise ValueError("channel installation, conversation, and event allowlists are required")
        if not 0 < timeout <= 3600:
            raise ValueError("timeout must be positive and at most 3600 seconds")
        if not math.isfinite(not_before) or not_before < 0:
            raise ValueError("not_before must be a finite non-negative Unix timestamp")
        self.scope = "channel:" + channel_identity(application_id, binding_id)
        self.binding, self.store, self.invoke, self.send = binding, store, invoke, send
        self.allowed_senders, self.allowed_kinds, self.timeout = allowed_senders, allowed_kinds, timeout
        self.not_before = not_before

    def accepts(self, event: ChannelEvent) -> bool:
        """Recheck policy at intake and execution, including work retained before changes."""
        checks = (
            event.platform == self.binding.platform,
            event.installation_id in self.binding.allowed_installations,
            event.conversation_id in self.binding.allowed_conversations,
            event.sender_id in self.allowed_senders,
            event.kind in self.allowed_kinds,
            event.occurred_at >= self.not_before,
            bool(event.content.strip()),
        )
        return all(checks)

    async def admit(self, event: ChannelEvent) -> str | None:
        """Commit input and dispatch intent in one atomic, idempotent task admission."""
        if not self.accepts(event):
            return None
        # Delivery IDs vary on broker redelivery and are not provider event identity.
        event = replace(event, delivery_id="")
        identity = channel_identity(self.scope, event.platform, event.installation_id, event.provider_event_id)
        actor = channel_identity(self.scope, event.platform, event.installation_id, event.sender_id)
        job = TaskRecord(
            job_id=identity, application_id=self.scope, user_id=actor, task_name="channel.invoke",
            queue="channel.inbox", arguments={"event": asdict(event)}, max_retries=0,
            idempotency_key=identity, trigger="channel", created_at=event.occurred_at,
        )
        await self.store.enqueue_task(job)
        return identity

    async def step(self) -> int:
        """Process bounded disjoint claims, prioritizing replies already durably queued."""
        count = 0
        for queue in ("channel.outbox", "channel.inbox"):
            jobs = await self.store.claim_tasks(
                application_id=self.scope, queues=(queue,), now=time.time(),
                lease_seconds=self.timeout + 30, limit=1,
            )
            for job in jobs:
                await self._execute(job)
                count += 1
        return count

    async def run(self, stop: asyncio.Event) -> None:
        """Drain bounded work until shutdown; the receiver must stop before this worker."""
        while not stop.is_set():
            if not await self.step():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=.25)
                except asyncio.TimeoutError:
                    pass

    async def _execute(self, job: TaskRecord) -> None:
        """Record a safe outcome without logging input, output, credentials, or exceptions."""
        failure = "delivery_unknown" if job.queue == "channel.outbox" else "execution_unknown"
        try:
            result = await asyncio.wait_for(self._perform(job), timeout=self.timeout)
        except asyncio.CancelledError:
            await self._finish(job, "failed", failure_code=failure)
            raise
        except Exception:
            await self._finish(job, "failed", failure_code=failure)
        else:
            await self._finish(job, "completed", result=result)

    async def _perform(self, job: TaskRecord) -> dict[str, Any]:
        """Apply current routing policy before either effect, using persisted addresses only."""
        event = ChannelEvent(**job.arguments["event"])
        if not self.accepts(event):
            return {"outcome": "policy_rejected"}
        if job.queue == "channel.outbox":
            receipt = await self.send(ChannelReply(**job.arguments["reply"]))
            return {"outcome": "sent", "receipt": receipt}
        return await self._invoke(job, event)

    async def _invoke(self, job: TaskRecord, event: ChannelEvent) -> dict[str, Any]:
        """Isolate sessions by agent, binding, installation, conversation, thread and actor."""
        session = channel_identity(self.scope, event.platform, event.installation_id,
                                   event.conversation_id, event.thread_id or event.message_id, event.sender_id)
        text = await self.invoke(event, session, job.user_id)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("channel invocation did not return a completed text response")
        if not await self.store.renew_task_lease(
            application_id=self.scope, job_id=job.job_id, lease_token=job.lease_token,
            now=time.time(), lease_seconds=30,
        ):
            raise RuntimeError("channel invocation no longer owns its work lease")
        reply = ChannelReply(
            reply_id=job.job_id, platform=event.platform, installation_id=event.installation_id,
            conversation_id=event.conversation_id,
            reply_to={"thread_id": event.thread_id or event.message_id}, content=text,
        )
        # Queuing precedes inbox completion: a crash cannot acknowledge a response
        # whose delivery intent exists only in process memory.
        await self.store.enqueue_task(TaskRecord(
            job_id="reply:" + job.job_id, application_id=self.scope, user_id=job.user_id,
            task_name="channel.reply", queue="channel.outbox", max_retries=0,
            arguments={"event": asdict(event), "reply": asdict(reply)},
            idempotency_key=job.job_id, trigger="channel",
        ))
        return {"outcome": "reply_queued"}

    async def _finish(self, job: TaskRecord, status: str, **outcome: Any) -> None:
        """Let the store reject stale owners after lease expiry or cancellation."""
        await self.store.finish_task(application_id=self.scope, job_id=job.job_id,
                                     lease_token=job.lease_token, now=time.time(), status=status, **outcome)
