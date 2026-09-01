"""Opt-in live database tests for built-in production stores."""

import os
import unittest
import uuid

from harnest.checkpoint import A2ATaskRecord, CheckpointRecord, RunScope
from harnest.continuation import ContinuationFailure, ContinuationProvider
from harnest.durable import ResumeArtifact
from harnest.store import PostgresStore, RedisStore


def _record(run_id: str) -> CheckpointRecord:
    return CheckpointRecord(
        run_id=run_id,
        checkpoint_id="checkpoint-1",
        namespace="graph",
        framework="langgraph",
        type_name="json",
        payload=b"{}",
        metadata_type="json",
        metadata=b"{}",
        versions_type="json",
        versions=b"{}",
    )


class _LiveStoreContract:
    store = None

    async def exercise_store(self):
        """Exercise shared session, checkpoint, continuation, and A2A contracts."""

        token = uuid.uuid4().hex
        user_id = f"integration-user-{token}"
        session_id = f"integration-session-{token}"
        run_id = f"integration-run-{token}"
        cancel_run_id = f"integration-cancel-{token}"
        application_id = f"integration-{token}"
        task_ids = (f"task-a-{token}", f"task-b-{token}")
        await self.store.start()
        try:
            await self.store.create(
                session_id=session_id, user_id=user_id, state={"count": 1}
            )
            async with self.store.acquire(
                session_id=session_id, user_id=user_id
            ) as lease:
                await lease.patch_state({"count": 2})
                await lease.replace_application_data(
                    {"private-note": {"status": "ready"}}
                )
                # Native framework replacement must operate on its lane only.
                await lease.replace_state({"count": 3})
            session = await self.store.get(
                session_id=session_id, user_id=user_id
            )
            self.assertEqual(session.state["count"], 3)
            self.assertEqual(
                session.application_data,
                {"private-note": {"status": "ready"}},
            )
            await self.store.begin_run(
                application_id=application_id,
                user_id=user_id,
                session_id=session_id,
                run_id=run_id,
                framework="langgraph",
            )
            scope = RunScope(application_id, user_id, session_id, run_id)
            stored = await self.store.put(
                _record(run_id), scope=scope, expected_revision=None
            )
            self.assertEqual(stored.payload, b"{}")
            provider = ContinuationProvider(
                self.store,
                application_id=application_id,
                provider="fake-worker",
            )
            suspended = await provider.suspend(
                user_id=user_id,
                session_id=session_id,
                run_id=run_id,
                external_id=f"external-{token}",
                capability="workflow.run",
                schema_id="integration-result/v1",
                resume=ResumeArtifact(
                    "langgraph", f"checkpoint-{token}", "call-1", "workflow"
                ),
            )
            self.assertEqual(
                (await provider.list_pending())[0].record, suspended
            )
            completed = await provider.complete(
                user_id=user_id,
                session_id=session_id,
                run_id=run_id,
                external_id=f"external-{token}",
                schema_id="integration-result/v1",
                result={"ok": True},
                validate=lambda value: value,
            )
            armed = await provider.arm(completed)
            claimed = await provider.claim(
                user_id=user_id,
                session_id=session_id,
                run_id=run_id,
                continuation_id=completed.continuation_id,
                expected_revision=armed.revision,
            )
            self.assertEqual(claimed.result, {"ok": True})
            await self.store.transition(
                scope=scope,
                expected_status="running",
                status="completed",
            )
            self.assertEqual(
                (await self.store.get_run(scope=scope)).status,
                "completed",
            )
            await self.store.delete_run(scope=scope)
            self.assertIsNone(
                await provider.get(
                    user_id=user_id,
                    session_id=session_id,
                    run_id=run_id,
                    continuation_id=claimed.continuation_id,
                )
            )
            await self._exercise_a2a_tasks(
                application_id=application_id,
                user_id=user_id,
                session_id=session_id,
                task_ids=task_ids,
            )
            await self._exercise_atomic_cancellation(
                provider=provider,
                application_id=application_id,
                user_id=user_id,
                session_id=session_id,
                run_id=cancel_run_id,
                token=token,
            )
        finally:
            await self.store.delete_run(
                scope=RunScope(application_id, user_id, session_id, run_id)
            )
            await self.store.delete_run(
                scope=RunScope(
                    application_id,
                    user_id,
                    f"{session_id}-cancel",
                    cancel_run_id,
                )
            )
            for task_id in task_ids:
                await self.store.delete_a2a_task(
                    application_id=application_id,
                    user_id=user_id,
                    task_id=task_id,
                )
            await self.store.delete(session_id=session_id, user_id=user_id)
            await self.store.close()

    async def _exercise_a2a_tasks(
        self, *, application_id, user_id, session_id, task_ids
    ):
        """Verify durable A2A owner isolation, filtering, paging, and deletion."""

        records = (
            A2ATaskRecord(
                application_id=application_id,
                user_id=user_id,
                task_id=task_ids[0],
                context_id=session_id,
                status=1,
                status_timestamp="2026-01-01T00:00:01+00:00",
                payload=b"task-a",
            ),
            A2ATaskRecord(
                application_id=application_id,
                user_id=user_id,
                task_id=task_ids[1],
                context_id=session_id,
                status=2,
                status_timestamp="2026-01-01T00:00:02+00:00",
                payload=b"task-b",
            ),
        )
        for record in records:
            await self.store.put_a2a_task(record)
        owned = await self.store.get_a2a_task(
            application_id=application_id,
            user_id=user_id,
            task_id=task_ids[0],
        )
        hidden = await self.store.get_a2a_task(
            application_id=application_id,
            user_id=f"foreign-{user_id}",
            task_id=task_ids[0],
        )
        first = await self.store.list_a2a_tasks(
            application_id=application_id,
            user_id=user_id,
            context_id=session_id,
            limit=1,
        )
        second = await self.store.list_a2a_tasks(
            application_id=application_id,
            user_id=user_id,
            context_id=session_id,
            cursor_task_id=first.records[0].task_id,
            limit=2,
        )
        filtered = await self.store.list_a2a_tasks(
            application_id=application_id,
            user_id=user_id,
            status=1,
        )

        self.assertEqual(owned.payload, b"task-a")
        self.assertIsNone(hidden)
        self.assertEqual(first.total_size, 2)
        self.assertEqual(len(first.records), 1)
        self.assertEqual(second.records[0].task_id, first.records[0].task_id)
        self.assertEqual(filtered.records, (owned,))
        self.assertTrue(
            await self.store.delete_a2a_task(
                application_id=application_id,
                user_id=user_id,
                task_id=task_ids[0],
            )
        )
        self.assertIsNone(
            await self.store.get_a2a_task(
                application_id=application_id,
                user_id=user_id,
                task_id=task_ids[0],
            )
        )

    async def _exercise_atomic_cancellation(
        self, *, provider, application_id, user_id, session_id, run_id, token
    ):
        """Prove one provider CAS cancels both its wait and durable run."""

        await self.store.begin_run(
            application_id=application_id,
            user_id=user_id,
            session_id=f"{session_id}-cancel",
            run_id=run_id,
            framework="langgraph",
        )
        waiting = await provider.suspend(
            user_id=user_id,
            session_id=f"{session_id}-cancel",
            run_id=run_id,
            external_id=f"cancel-external-{token}",
            capability="workflow.cancel",
            schema_id="integration-cancel/v1",
            resume=ResumeArtifact(
                "langgraph", f"cancel-checkpoint-{token}", "call-2", "workflow"
            ),
        )
        cancelled = await provider.cancel(
            waiting, ContinuationFailure("transport_cancelled")
        )
        run = await self.store.get_run(
            scope=RunScope(
                application_id,
                user_id,
                f"{session_id}-cancel",
                run_id,
            )
        )

        self.assertEqual(cancelled.status, "failed")
        self.assertEqual(cancelled.failure.code, "transport_cancelled")
        self.assertEqual(run.status, "cancelled")
        self.assertIsNone(run.pending_action)


@unittest.skipUnless(
    os.getenv("HARNEST_TEST_POSTGRES_URL"),
    "set HARNEST_TEST_POSTGRES_URL for the live Postgres store test",
)
class LivePostgresStoreTests(_LiveStoreContract, unittest.IsolatedAsyncioTestCase):
    async def test_live_session_checkpoint_and_cas(self):
        self.store = PostgresStore(os.environ["HARNEST_TEST_POSTGRES_URL"])
        await self.exercise_store()


@unittest.skipUnless(
    os.getenv("HARNEST_TEST_REDIS_URL"),
    "set HARNEST_TEST_REDIS_URL for the live Redis store test",
)
class LiveRedisStoreTests(_LiveStoreContract, unittest.IsolatedAsyncioTestCase):
    async def test_live_session_checkpoint_and_cas(self):
        self.store = RedisStore(
            os.environ["HARNEST_TEST_REDIS_URL"],
            prefix=f"harnest-integration-{uuid.uuid4().hex}",
        )
        await self.exercise_store()


if __name__ == "__main__":
    unittest.main()
