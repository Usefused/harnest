from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from harnest.checkpoint import MemoryStore, RunScope
from harnest.continuation import (
    ContinuationConflictError,
    ContinuationFailure,
    ContinuationProvider,
    ContinuationStore,
    ContinuationValidationError,
    continuation_schema_id,
)
from harnest.durable import ResumeArtifact


_RESUME = ResumeArtifact(
    framework="langgraph",
    native_invocation_id="reports/user-1/session-1",
    tool_call_id="call-1",
    tool_name="report",
)


class MemoryContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MemoryStore()
        await self.store.start()
        await self.store.create(session_id="session-1", user_id="user-1", state={})
        await self.store.begin_run(
            application_id="reports",
            user_id="user-1",
            session_id="session-1",
            run_id="run-1",
            framework="langgraph",
        )
        self.provider = ContinuationProvider(
            self.store, application_id="reports", provider="hatchet"
        )

    async def asyncTearDown(self):
        await self.store.close()

    async def _suspend(self, external_id: str = "job-1"):
        return await self.provider.suspend(
            user_id="user-1",
            session_id="session-1",
            run_id="run-1",
            external_id=external_id,
            capability="workflow.run",
            schema_id="report-result/v1",
            resume=_RESUME,
        )

    def _scope(self, *, user_id: str = "user-1") -> RunScope:
        return RunScope("reports", user_id, "session-1", "run-1")

    async def test_store_contract_and_suspend_keep_external_id_private(self):
        self.assertIsInstance(self.store, ContinuationStore)

        record = await self._suspend()
        run = await self.store.get_run(scope=self._scope())
        pending = await self.provider.list_pending()

        self.assertEqual(run.status, "waiting")
        self.assertEqual(run.pending_action.type, "external_continuation")
        self.assertEqual(run.pending_action.action_id, record.continuation_id)
        self.assertNotIn("job-1", repr(run.pending_action))
        self.assertEqual(pending[0].record, record)
        self.assertEqual(pending[0].external_id, "job-1")

    async def test_completion_is_validated_then_claimed_exactly_once(self):
        suspended = await self._suspend()

        with self.assertRaises(ContinuationValidationError):
            await self.provider.complete(
                user_id="user-1",
                session_id="session-1",
                run_id="run-1",
                external_id="job-1",
                schema_id="report-result/v1",
                result={"count": "wrong"},
                validate=lambda value: (_ for _ in ()).throw(ValueError()),
            )
        self.assertEqual(
            (await self.provider.get(
                user_id="user-1",
                session_id="session-1",
                run_id="run-1",
                continuation_id=suspended.continuation_id,
            )).status,
            "pending",
        )

        completed = await self.provider.complete(
            user_id="user-1",
            session_id="session-1",
            run_id="run-1",
            external_id="job-1",
            schema_id="report-result/v1",
            result={"count": 2},
            validate=lambda value: {"count": int(value["count"])},
        )
        completed = await self.provider.arm(completed)
        claimed = await self.provider.claim(
            user_id="user-1",
            session_id="session-1",
            run_id="run-1",
            continuation_id=completed.continuation_id,
            expected_revision=completed.revision,
        )

        self.assertEqual(claimed.status, "claimed")
        self.assertEqual(claimed.result, {"count": 2})
        self.assertEqual((await self.store.get_run(scope=self._scope())).status, "running")
        with self.assertRaises(ContinuationConflictError):
            await self.provider.claim(
                user_id="user-1",
                session_id="session-1",
                run_id="run-1",
                continuation_id=completed.continuation_id,
                expected_revision=completed.revision,
            )

    async def test_provider_and_run_ownership_fail_closed(self):
        suspended = await self._suspend()
        foreign = ContinuationProvider(
            self.store, application_id="reports", provider="temporal"
        )

        with self.assertRaises(ContinuationConflictError):
            await foreign.complete(
                user_id="user-1",
                session_id="session-1",
                run_id="run-1",
                external_id="job-1",
                schema_id="report-result/v1",
                result={},
                validate=lambda value: value,
            )
        self.assertIsNone(
            await self.store.get_continuation(
                scope=self._scope(user_id="other-user"),
                continuation_id=suspended.continuation_id,
            )
        )

    async def test_duplicate_external_identity_and_concurrent_resolution_cas(self):
        await self._suspend()
        with self.assertRaises(ContinuationConflictError):
            await self.provider.suspend(
                user_id="user-1",
                session_id="session-1",
                run_id="run-1",
                external_id="job-1",
                capability="workflow.run",
                schema_id="report-result/v1",
                resume=_RESUME,
            )

        outcomes = await asyncio.gather(
            self.provider.complete(
                user_id="user-1",
                session_id="session-1",
                run_id="run-1",
                external_id="job-1",
                schema_id="report-result/v1",
                result={"winner": 1},
                validate=lambda value: value,
            ),
            self.provider.fail(
                user_id="user-1",
                session_id="session-1",
                run_id="run-1",
                external_id="job-1",
                schema_id="report-result/v1",
                failure=ContinuationFailure("worker.failed", retryable=True),
            ),
            return_exceptions=True,
        )
        self.assertEqual(sum(not isinstance(value, Exception) for value in outcomes), 1)
        self.assertEqual(
            sum(isinstance(value, ContinuationConflictError) for value in outcomes),
            1,
        )

    async def test_reconciliation_is_bounded_and_keyset_paginated(self):
        first = await self._suspend()
        await self.store.transition(
            scope=self._scope(), expected_status="waiting", status="cancelled"
        )
        await self.store.begin_run(
            application_id="reports",
            user_id="user-1",
            session_id="session-1",
            run_id="run-2",
            framework="langgraph",
        )
        second = await self.provider.suspend(
            user_id="user-1",
            session_id="session-1",
            run_id="run-2",
            external_id="job-2",
            capability="workflow.run",
            schema_id="report-result/v1",
            resume=_RESUME,
        )

        page = await self.provider.list_pending(after=first.continuation_id, limit=1)
        expected = second if second.continuation_id > first.continuation_id else None
        self.assertEqual(tuple(value.record for value in page), (() if expected is None else (expected,)))
        with self.assertRaises(ValueError):
            await self.provider.list_pending(limit=1001)

    async def test_failure_and_audit_never_log_external_or_result_payload(self):
        suspended = await self._suspend("private-customer-job")
        audit = unittest.mock.Mock()
        with patch("harnest.continuation._AUDIT", audit):
            failed = await self.provider.fail(
                user_id="user-1",
                session_id="session-1",
                run_id="run-1",
                external_id="private-customer-job",
                schema_id="report-result/v1",
                failure=ContinuationFailure("worker.timeout", retryable=True),
            )

        self.assertEqual(failed.failure.code, "worker.timeout")
        logged = repr(audit.info.call_args_list)
        self.assertNotIn("private-customer-job", logged)
        self.assertNotIn(suspended.continuation_id, logged)
        self.assertNotIn("worker.timeout", logged)

    async def test_deleting_run_removes_private_continuation_mapping(self):
        suspended = await self._suspend()
        await self.store.delete_run(scope=self._scope())

        self.assertIsNone(
            await self.provider.get(
                user_id="user-1",
                session_id="session-1",
                run_id="run-1",
                continuation_id=suspended.continuation_id,
            )
        )
        self.assertEqual(await self.provider.list_pending(), ())


class ContinuationDomainTests(unittest.TestCase):
    def test_schema_fingerprint_is_canonical_and_never_contains_source_type(self):
        left = continuation_schema_id({"type": "object", "required": ["value"]})
        right = continuation_schema_id({"required": ["value"], "type": "object"})

        self.assertEqual(left, right)
        self.assertTrue(left.startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
