import asyncio
import unittest

from harnest.checkpoint import (
    CheckpointConflictError,
    CheckpointRecord,
    CheckpointWrite,
    MemoryStore,
    PendingAction,
    RunScope,
)


def _checkpoint(run_id: str, checkpoint_id: str = "checkpoint-1") -> CheckpointRecord:
    return CheckpointRecord(
        run_id=run_id,
        checkpoint_id=checkpoint_id,
        namespace="",
        framework="langgraph",
        type_name="json",
        payload=b"{}",
        metadata_type="json",
        metadata=b"{}",
        versions_type="json",
        versions=b"{}",
    )


class MemoryCheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MemoryStore()
        await self.store.start()

    async def asyncTearDown(self):
        await self.store.close()

    async def _begin(self, run_id="run-1"):
        return await self.store.begin_run(
            application_id="support",
            user_id="user-1",
            session_id="session-1",
            run_id=run_id,
            framework="langgraph",
        )

    def _scope(self, run_id="run-1", *, user_id="user-1"):
        return RunScope("support", user_id, "session-1", run_id)

    async def test_compare_and_swap_rejects_stale_checkpoint_write(self):
        await self._begin()
        first = await self.store.put(
            _checkpoint("run-1"), scope=self._scope(), expected_revision=None
        )

        self.assertEqual(first.revision, 0)
        with self.assertRaisesRegex(CheckpointConflictError, "revision changed"):
            await self.store.put(
                _checkpoint("run-1"),
                scope=self._scope(),
                expected_revision=None,
            )

        updated = await self.store.put(
            _checkpoint("run-1"),
            scope=self._scope(),
            expected_revision=first.revision,
        )
        self.assertEqual(updated.revision, 1)

    async def test_one_active_run_per_session_and_terminal_release(self):
        await self._begin()
        with self.assertRaisesRegex(CheckpointConflictError, "active"):
            await self._begin("run-2")

        await self.store.transition(
            scope=self._scope(), expected_status="running", status="completed"
        )
        resumed = await self._begin("run-2")

        self.assertEqual(resumed.status, "running")

    async def test_wait_resume_uses_atomic_status_transitions(self):
        await self._begin()
        pending = PendingAction("human_approval", "approval-1", "deploy")

        waiting = await self.store.transition(
            scope=self._scope(),
            expected_status="running",
            status="waiting",
            pending_action=pending,
        )
        running = await self.store.transition(
            scope=self._scope(), expected_status="waiting", status="running"
        )

        self.assertEqual(waiting.pending_action, pending)
        self.assertIsNone(running.pending_action)
        with self.assertRaises(CheckpointConflictError):
            await self.store.transition(
                scope=self._scope(), expected_status="waiting", status="running"
            )

    async def test_concurrent_status_cas_has_one_winner(self):
        await self._begin()

        results = await asyncio.gather(
            self.store.transition(
                scope=self._scope(), expected_status="running", status="completed"
            ),
            self.store.transition(
                scope=self._scope(), expected_status="running", status="failed"
            ),
            return_exceptions=True,
        )

        self.assertEqual(sum(not isinstance(item, Exception) for item in results), 1)
        self.assertEqual(
            sum(isinstance(item, CheckpointConflictError) for item in results), 1
        )

    async def test_pending_writes_and_delete_cleanup(self):
        await self._begin()
        await self.store.put(
            _checkpoint("run-1"), scope=self._scope(), expected_revision=None
        )
        write = CheckpointWrite("task-1", "messages", "json", b"[]")
        await self.store.put_writes(
            scope=self._scope(), checkpoint_id="checkpoint-1", writes=(write,)
        )

        self.assertEqual(
            await self.store.get_writes(
                scope=self._scope(), checkpoint_id="checkpoint-1"
            ),
            (write,),
        )
        await self.store.delete_run(scope=self._scope())

        self.assertIsNone(await self.store.get_run(scope=self._scope()))
        self.assertIsNone(await self.store.get_checkpoint(scope=self._scope()))

    async def test_foreign_scope_cannot_read_mutate_or_delete_run(self):
        await self._begin()
        await self.store.put(
            _checkpoint("run-1"), scope=self._scope(), expected_revision=None
        )
        foreign = self._scope(user_id="other-user")

        self.assertIsNone(await self.store.get_run(scope=foreign))
        self.assertIsNone(await self.store.get_checkpoint(scope=foreign))
        with self.assertRaises(KeyError):
            await self.store.transition(
                scope=foreign,
                expected_status="running",
                status="completed",
            )
        await self.store.delete_run(scope=foreign)
        self.assertIsNotNone(await self.store.get_run(scope=self._scope()))


if __name__ == "__main__":
    unittest.main()
