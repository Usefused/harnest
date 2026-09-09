from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
import os
import threading
import time
import unittest
from unittest.mock import patch
import uuid

import asyncpg
from a2a import helpers
from a2a.types import Role, SendMessageConfiguration, SendMessageRequest
from fastapi.testclient import TestClient
from google.protobuf.json_format import MessageToDict

from harnest.application import CompiledApplication
from harnest import context, cron
from harnest.agent import tool
from harnest.agent_principal import (
    AgentRuntimePrincipal,
    activate_agent_principal,
    active_agent_principal,
    create_agent_principal_binding,
    revoke_agent_principal,
)
from harnest.context import activate_context, create_agent_context, revoke_context
from harnest.cron import CompiledCron
from harnest.durable import NativeResumeInput, ResumeArtifact, native_durable_call
from harnest.external_continuation import ExternalContinuationRuntime
from harnest.external_continuation_driver import ExternalContinuationRuntimeDriver
from harnest.neutral_runtime import create_neutral_app
from harnest.runtime_a2a_store import HarnestA2ATaskStore
from harnest.runtime_contract import InvocationRequest, InvocationResult
from harnest.runtime_pipeline import build_runtime_pipeline
from harnest.runtime_task import (
    TaskRuntimeDriver,
    TaskRuntimeManager,
)
from harnest.store import PostgresStore
from harnest.task import CompiledTask, registration_for, task

from test_neutral_runtime import FakeDriver


_POSTGRES_DSN = os.environ.get("HARNEST_TEST_POSTGRES_DSN")


def _a2a_card() -> dict[str, object]:
    """Return the protocol surface used by the live durable-task fixture."""

    return {
        "name": "Durable task fixture",
        "description": "Exercises A2A through a real Harnest task worker.",
        "version": "1.0.0",
        "supportedInterfaces": [
            {
                "url": "http://testserver/a2a",
                "protocolBinding": "HTTP+JSON",
                "protocolVersion": "1.0",
            }
        ],
        "capabilities": {"streaming": True},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": [
            {
                "id": "deliver",
                "name": "Deliver",
                "description": "Waits for a durable Harnest task.",
                "tags": ["integration"],
            }
        ],
    }


class _A2ADurableTaskDriver(FakeDriver):
    """Enter a real ``@task`` wait and consume its ADK-style durable resume."""

    def __init__(
        self,
        authored_task,
        *,
        store: PostgresStore,
        application_id: str,
        schedule_in: float,
    ) -> None:
        """Use one application identity for both native and outer storage ownership."""

        super().__init__()
        self.authored_task = authored_task
        self.store = store
        self.application_id = application_id
        self.schedule_in = schedule_in
        self.info = replace(
            self.info,
            id=application_id,
            card=_a2a_card(),
            framework="adk",
            mode="managed",
        )

    async def stream(self, request: InvocationRequest):
        """Suspend the initial A2A turn on one real queued task result."""

        await self.store.begin_run(
            application_id=self.application_id,
            user_id=request.user_id,
            session_id=request.session_id,
            run_id=request.invocation_id,
            framework="adk",
        )
        artifact = ResumeArtifact(
            "adk", request.invocation_id, "task-call", "deliver"
        )
        with native_durable_call(artifact):
            handle = await self.authored_task.defer(
                request.input,
                schedule_in=self.schedule_in,
            )
            value = await handle.result()
        for event in _result_events(value):
            yield event

    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        """Return the resumed result; outer storage owns durable completion."""

        if not isinstance(request.input, NativeResumeInput):
            raise AssertionError("durable callback did not use NativeResumeInput")
        value = request.input.value
        events = _result_events(value)
        # Match native adapters: StorageRuntimeDriver commits the result before
        # transitioning the run. Completing here exposes a result-less terminal
        # window and prevents the outer wrapper from persisting its checkpoint.
        return InvocationResult(
            text=f"delivered: {value}",
            events=events,
            result=value,
            session_id=request.session_id,
            metadata=request.metadata,
        )


class _DynamicCronToolDriver(FakeDriver):
    """Invoke one real authored tool inside the normal managed context wrapper."""

    def __init__(self, callback) -> None:
        super().__init__()
        self.info = replace(self.info, framework="langgraph")
        self.callback = callback

    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        """Let the test select create or cancel without bypassing tool governance."""

        value = await self.callback(request.input)
        return InvocationResult(
            text=str(value),
            events=(),
            result=value,
            session_id=request.session_id,
            metadata=request.metadata,
        )


def _result_events(value: object) -> tuple[dict[str, object], ...]:
    """Build the stable public result projected back into the A2A task."""

    return (
        {"type": "message", "role": "assistant", "text": f"delivered: {value}"},
        {"type": "output", "value": value},
    )


def _send_payload(value: str = "hello") -> dict[str, object]:
    """Request asynchronous task semantics through the official A2A model."""

    message = helpers.new_text_message(value, role=Role.ROLE_USER)
    return MessageToDict(
        SendMessageRequest(
            message=message,
            configuration=SendMessageConfiguration(
                history_length=0,
                return_immediately=True,
            ),
        )
    )


def _a2a_headers() -> dict[str, str]:
    """Use the media type and protocol version required by A2A 1.0."""

    return {
        "A2A-Version": "1.0",
        "Content-Type": "application/a2a+json",
    }


def _compiled_a2a_application(
    authored_task,
    *,
    store: PostgresStore,
    application_id: str,
    task_name: str,
) -> CompiledApplication:
    """Compile one task fixture against the production PostgreSQL store."""

    definition = registration_for(authored_task)
    assert definition is not None
    compiled = CompiledTask(
        name=task_name,
        source="tasks/deliver.py",
        definition=definition,
        authored=authored_task,
    )
    return CompiledApplication(
        name=application_id,
        framework="adk",
        mode="managed",
        target=object(),
        tasks=(compiled,),
        checkpointer=store,
    )


def _a2a_task_app(
    authored_task,
    *,
    application_id: str,
    task_name: str,
    schedule_in: float,
):
    """Build the production wrapper order around one live task runtime."""

    store = PostgresStore(_POSTGRES_DSN)
    application = _compiled_a2a_application(
        authored_task,
        store=store,
        application_id=application_id,
        task_name=task_name,
    )
    inner = _A2ADurableTaskDriver(
        authored_task,
        store=store,
        application_id=application_id,
        schedule_in=schedule_in,
    )
    pipeline = build_runtime_pipeline(
        inner,
        application.runtime_capabilities,
        (),
    )
    continuations = ExternalContinuationRuntime(
        store, application_id=application_id
    )
    pipeline = ExternalContinuationRuntimeDriver(pipeline, continuations)
    pipeline = TaskRuntimeDriver(
        pipeline,
        TaskRuntimeManager(application, continuation_runtime=continuations),
    )
    continuations.bind_driver(pipeline)
    app = create_neutral_app(
        pipeline,
        playground_enabled=False,
        a2a_task_store=HarnestA2ATaskStore(
            store, application_id=application_id
        ),
    )
    return app


def _wait_for_a2a_state(client, task_id: str, expected: str) -> dict[str, object]:
    """Bound polling while allowing the native worker's five-second fetch interval."""

    deadline = time.monotonic() + 15
    last_status, last_state = None, None
    terminal = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"}
    while time.monotonic() < deadline:
        response = client.get(f"/a2a/tasks/{task_id}", headers=_a2a_headers())
        last_status = response.status_code
        if response.status_code == 200:
            task_value = response.json()
            last_state = task_value["status"]["state"]
            if last_state == expected:
                return task_value
            if last_state in terminal:
                break
        time.sleep(0.05)
    # Do not include response bodies: artifacts and error details may be private.
    raise AssertionError(f"A2A task did not reach {expected}; last HTTP status={last_status}, state={last_state}")


@contextmanager
def _hold_a2a_completion():
    """Expose the backend-return window deterministically, without delaying the database."""

    returned, release = threading.Event(), threading.Event()
    original = _A2ADurableTaskDriver.invoke

    async def invoke(driver, request):
        """Hold only the test backend's result before the outer storage commit."""

        result = await original(driver, request)
        returned.set()
        if not await asyncio.to_thread(release.wait, 15):
            raise AssertionError("test did not release the A2A completion gate")
        return result

    try:
        with patch.object(_A2ADurableTaskDriver, "invoke", invoke):
            yield returned, release
    finally:
        release.set()


def _assert_a2a_completion_pending(test, client, task_id, gate):
    """A poll before result persistence must not permanently publish failure."""

    returned, release = gate
    try:
        test.assertTrue(returned.wait(15), "native resume did not return a result")
        response = client.get(f"/a2a/tasks/{task_id}", headers=_a2a_headers())
        test.assertEqual(response.status_code, 200)
        test.assertEqual(response.json()["status"]["state"], "TASK_STATE_WORKING")
        durable = asyncio.run(_continuation_database_evidence(task_id))
        test.assertEqual(durable["run_status"], "running")
        test.assertEqual(durable["continuation_status"], "claimed")
    finally:
        release.set()


async def _task_database_evidence(task_name: str) -> dict[str, object]:
    """Read committed queue and private payload state without runtime internals."""

    connection = await asyncpg.connect(_POSTGRES_DSN)
    try:
        row = await connection.fetchrow(
            """
            SELECT payload.payload_id,
                   payload.status AS payload_status,
                   payload.failure_code,
                   payload.arguments::text AS arguments,
                   payload.invocation::text AS invocation,
                   payload.agent_permissions::text AS agent_permissions,
                   job.id AS job_id,
                   job.status::text AS job_status
            FROM harnest_task_payloads AS payload
            JOIN procrastinate_jobs AS job
              ON job.task_name=payload.task_name
             AND job.args->>'_harnest_payload_id'=payload.payload_id
            WHERE payload.task_name=$1
            ORDER BY payload.created_at DESC
            LIMIT 1
            """,
            task_name,
        )
        if row is None:
            raise AssertionError("durable task database row was not committed")
        return dict(row)
    finally:
        await connection.close()


async def _continuation_database_evidence(run_id: str) -> dict[str, object]:
    """Read the run and continuation rows changed by transport cancellation."""

    connection = await asyncpg.connect(_POSTGRES_DSN)
    try:
        for _attempt in range(100):
            row = await connection.fetchrow(
                """
                SELECT run.status AS run_status,
                       run.pending_action::text AS pending_action,
                       continuation.status AS continuation_status,
                       continuation.failure::text AS failure
                FROM harnest_runs AS run
                JOIN harnest_continuations AS continuation USING (run_id)
                WHERE run.run_id=$1
                """,
                run_id,
            )
            if row is not None:
                return dict(row)
            await asyncio.sleep(0.05)
        raise AssertionError("durable continuation rows were not committed")
    finally:
        await connection.close()


async def _dynamic_cron_database_evidence(
    application_id: str, schedule_id: str, task_name: str
) -> dict[str, object]:
    """Wait for the real worker and return only ownership and occurrence state."""

    connection = await asyncpg.connect(_POSTGRES_DSN)
    try:
        for _attempt in range(100):
            row = await connection.fetchrow(
                """
                SELECT cron.user_id, cron.status,
                       count(payload.payload_id)::int AS occurrence_count,
                       count(payload.payload_id) FILTER (
                           WHERE payload.status='completed'
                       )::int AS completed_count
                FROM harnest_cron_jobs AS cron
                LEFT JOIN harnest_task_payloads AS payload
                  ON payload.task_name=$3 AND payload.trigger='cron'
                WHERE cron.application_id=$1 AND cron.schedule_id=$2
                GROUP BY cron.user_id, cron.status
                """,
                application_id,
                schedule_id,
                task_name,
            )
            if row is not None and row["completed_count"]:
                return dict(row)
            await asyncio.sleep(0.05)
        raise AssertionError("dynamic cron occurrence did not complete")
    finally:
        await connection.close()


@unittest.skipUnless(_POSTGRES_DSN, "requires HARNEST_TEST_POSTGRES_DSN")
class ProcrastinateTaskIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_creates_gets_and_cancels_user_owned_dynamic_cron(self):
        """Cross Tool, managed context, PostgreSQL, scheduler, and worker boundaries."""

        completed = asyncio.Event()
        observed = []

        @task(queue="integration")
        async def deliver(value):
            """Record the identity reconstructed for a dynamic occurrence."""

            principal = active_agent_principal()
            observed.append(
                (
                    value,
                    context.user_id,
                    None if principal is None else principal.permissions,
                )
            )
            completed.set()

        definition = registration_for(deliver)
        assert definition is not None
        unique = uuid.uuid4().hex
        application_id = f"dynamic_cron_{unique}"
        task_name = f"harnest.{application_id}.tasks.deliver"
        compiled = CompiledTask(
            name=task_name,
            source="tasks/deliver.py",
            definition=definition,
            authored=deliver,
        )
        application = CompiledApplication(
            name=application_id,
            framework="langgraph",
            mode="managed",
            target=object(),
            tasks=(compiled,),
        )
        created_id = None

        @tool
        async def manage_cron(action: str) -> dict[str, object]:
            """Create or cancel the current user's integration schedule."""

            nonlocal created_id
            if action == "create":
                job = await cron.create(
                    key="weekly-report",
                    expression="* 8 15 1 *",
                    task=deliver,
                    arguments={"value": "private-dynamic-value"},
                )
                created_id = job.id
                return {"id": job.id, "count": len(await cron.list())}
            assert created_id is not None
            job = await cron.get(created_id)
            assert job is not None
            cancelled = await job.cancel()
            return {"id": cancelled.id, "status": cancelled.status}

        inner = _DynamicCronToolDriver(manage_cron)
        pipeline = build_runtime_pipeline(
            inner, application.runtime_capabilities, ()
        )
        manager = TaskRuntimeManager(application)
        driver = TaskRuntimeDriver(pipeline, manager)
        create_request = InvocationRequest(
            input="create",
            user_id="live-cron-user",
            session_id=f"session-{unique}",
            invocation_id=f"create-{unique}",
            metadata={},
            state_delta={},
        )
        try:
            with patch.dict(
                os.environ, {"HARNEST_TASK_DATABASE_URL": _POSTGRES_DSN}
            ):
                created = await driver.invoke(create_request)
                assert created_id is not None
                timestamp = 1_800_000_000
                await manager.cron_runtime.dispatch(timestamp)
                await asyncio.wait_for(completed.wait(), timeout=10)
                evidence = await _dynamic_cron_database_evidence(
                    application_id, created_id, task_name
                )
                cancelled = await driver.invoke(
                    replace(
                        create_request,
                        input="cancel",
                        invocation_id=f"cancel-{unique}",
                    )
                )
                await manager.cron_runtime.dispatch(timestamp + 60)
                after_cancel = await _dynamic_cron_database_evidence(
                    application_id, created_id, task_name
                )
            self.assertEqual(created.result["count"], 1)
            self.assertEqual(cancelled.result["status"], "cancelled")
            self.assertEqual(evidence["user_id"], "live-cron-user")
            self.assertEqual(evidence["occurrence_count"], 1)
            self.assertEqual(after_cancel["occurrence_count"], 1)
            self.assertEqual(
                observed,
                [("private-dynamic-value", "live-cron-user", frozenset())],
            )
        finally:
            await driver.close()

    async def test_cron_dispatcher_commits_one_private_job_per_occurrence(self):
        """Exercise cron idempotency and opaque payloads against real PostgreSQL."""

        completed = asyncio.Event()

        @task(queue="integration")
        async def deliver(value):
            """Deliver one scheduled integration payload."""

            completed.set()
            return value

        definition = registration_for(deliver)
        assert definition is not None
        unique = uuid.uuid4().hex
        compiled = CompiledTask(
            name=f"harnest.integration_{unique}.tasks.deliver",
            source="tasks/deliver.py",
            definition=definition,
            authored=deliver,
        )
        schedule = CompiledCron(
            name=f"harnest.integration_{unique}.cron.daily",
            source="cron/daily.py",
            schedule="0 0 * * *",
            timezone="UTC",
            task=compiled,
            arguments={"value": "private-cron-value"},
        )
        application = CompiledApplication(
            name=f"integration_{unique}",
            framework="langgraph",
            mode="managed",
            target=object(),
            tasks=(compiled,),
            crons=(schedule,),
        )
        manager = TaskRuntimeManager(application)
        try:
            with patch.dict(
                os.environ, {"HARNEST_TASK_DATABASE_URL": _POSTGRES_DSN}
            ), self.assertLogs("procrastinate", level="INFO") as native_logs:
                await manager.start()
                await manager._enqueue_cron(schedule, 1_800_000_000)
                await manager._enqueue_cron(schedule, 1_800_000_000)
                await asyncio.wait_for(completed.wait(), timeout=10)
                jobs = tuple(
                    await manager._app.job_manager.list_jobs_async(
                        task=compiled.name
                    )
                )
                self.assertEqual(len(jobs), 1)
                payload_id = jobs[0].task_kwargs["_harnest_payload_id"]
                handle = manager._handle(
                    compiled, int(jobs[0].id), payload_id, "cron"
                )
                await _wait_for_status(handle, "succeeded")
            self.assertEqual(await handle.result(), "private-cron-value")
            self.assertNotIn("private-cron-value", " ".join(native_logs.output))
        finally:
            await manager.close()

    async def test_real_worker_reconstructs_deferred_agent_permissions(self):
        """Carry invocation grants through PostgreSQL into a fresh worker scope."""

        observed = []
        completed = asyncio.Event()

        @task(queue="integration")
        async def inspect_principal():
            """Record the principal restored around the durable task attempt."""

            principal = active_agent_principal()
            observed.append(None if principal is None else principal.permissions)
            completed.set()

        definition = registration_for(inspect_principal)
        assert definition is not None
        unique = uuid.uuid4().hex
        compiled = CompiledTask(
            name=f"harnest.integration_{unique}.tasks.inspect_principal",
            source="tasks/inspect_principal.py",
            definition=definition,
            authored=inspect_principal,
        )
        application = CompiledApplication(
            name=f"integration_{unique}",
            framework="langgraph",
            mode="managed",
            target=object(),
            tasks=(compiled,),
        )
        manager = TaskRuntimeManager(application)
        context_value = create_agent_context(
            framework="langgraph",
            agent_name="integration",
            invocation_id=f"inv-{unique}",
            user_id="integration-user",
            session_id=f"session-{unique}",
            metadata={},
            resources={},
        )
        principal = AgentRuntimePrincipal.create(
            permissions={"reports.read", "reports.compare"}
        )
        binding = create_agent_principal_binding(principal)
        try:
            with patch.dict(
                os.environ, {"HARNEST_TASK_DATABASE_URL": _POSTGRES_DSN}
            ):
                await manager.start()
                with activate_context(context_value), activate_agent_principal(binding):
                    handle = await inspect_principal.defer()
                await asyncio.wait_for(completed.wait(), timeout=10)
                await _wait_for_status(handle, "succeeded")
            self.assertEqual(observed, [principal.permissions])
        finally:
            revoke_agent_principal(binding)
            revoke_context(context_value)
            await manager.close()

    async def test_real_worker_schedules_retries_and_hides_payload_from_logs(self):
        """Exercise the native queue against Postgres without a model runtime."""

        attempts = []
        completed = asyncio.Event()

        @task(queue="integration", max_retries=2)
        async def deliver(value):
            """Deliver one integration-test payload."""

            attempts.append(value)
            if len(attempts) == 1:
                raise RuntimeError(value)
            completed.set()
            return {"delivered": value}

        definition = registration_for(deliver)
        assert definition is not None
        unique = uuid.uuid4().hex
        compiled = CompiledTask(
            name=f"harnest.integration_{unique}.tasks.deliver",
            source="tasks/deliver.py",
            definition=definition,
            authored=deliver,
        )
        application = CompiledApplication(
            name=f"integration_{unique}",
            framework="langgraph",
            mode="managed",
            target=object(),
            tasks=(compiled,),
        )
        manager = TaskRuntimeManager(application)
        started = time.monotonic()
        try:
            with patch.dict(
                os.environ, {"HARNEST_TASK_DATABASE_URL": _POSTGRES_DSN}
            ), self.assertLogs("procrastinate", level="INFO") as native_logs:
                await manager.start()
                handle = await deliver.defer("private-task-value", schedule_in=0.2)
                await asyncio.wait_for(completed.wait(), timeout=10)
                status = await _wait_for_status(handle, "succeeded")
            self.assertGreaterEqual(time.monotonic() - started, 0.15)
            self.assertEqual(attempts, ["private-task-value", "private-task-value"])
            self.assertEqual(status, "succeeded")
            self.assertEqual(
                await handle.result(), {"delivered": "private-task-value"}
            )
            self.assertNotIn("private-task-value", " ".join(native_logs.output))
            native_job = manager._app.job_manager
            jobs = tuple(await native_job.list_jobs_async(id=int(handle.id)))
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].attempts, 2)
        finally:
            await manager.close()


@unittest.skipUnless(_POSTGRES_DSN, "requires HARNEST_TEST_POSTGRES_DSN")
class A2ADurableTaskIntegrationTests(unittest.TestCase):
    def test_completed_task_is_retrieved_by_a_fresh_a2a_replica(self):
        """Cross A2A, Procrastinate, PostgreSQL, and a restarted app boundary."""

        # Native workers select by queue, not task name. Keep independent
        # fixtures from claiming jobs whose callables they have not registered.
        unique = uuid.uuid4().hex

        @task(queue=f"a2a-{unique}")
        async def deliver(value):
            """Return one value from the real task worker."""

            return {"delivered": value}

        application_id = f"a2a_completion_{unique}"
        task_name = f"harnest.{application_id}.tasks.deliver"
        first_app = _a2a_task_app(
            deliver,
            application_id=application_id,
            task_name=task_name,
            schedule_in=0.05,
        )
        with _hold_a2a_completion() as gate, TestClient(first_app) as client:
            submitted = client.post(
                "/a2a/message:send",
                json=_send_payload("private-value"),
                headers=_a2a_headers(),
            )
            self.assertEqual(submitted.status_code, 200)
            task_id = submitted.json()["task"]["id"]
            _assert_a2a_completion_pending(self, client, task_id, gate)
            completed = _wait_for_a2a_state(
                client, task_id, "TASK_STATE_COMPLETED"
            )
            durable = asyncio.run(_continuation_database_evidence(task_id))

        second_app = _a2a_task_app(
            deliver,
            application_id=application_id,
            task_name=task_name,
            schedule_in=0.05,
        )
        with TestClient(second_app) as client:
            recovered = client.get(
                f"/a2a/tasks/{task_id}", headers=_a2a_headers()
            )

        self.assertEqual(completed["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertIn("private-value", repr(completed["artifacts"]))
        self.assertEqual(durable["run_status"], "completed")
        self.assertEqual(durable["continuation_status"], "claimed")
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(recovered.json(), completed)

    def test_a2a_cancel_atomically_stops_real_task_and_durable_wait(self):
        """Assert transport cancellation against committed database state."""

        unique = uuid.uuid4().hex

        @task(queue=f"a2a-{unique}")
        async def deliver(value):
            """Remain queued long enough for transport cancellation."""

            return {"delivered": value}

        application_id = f"a2a_cancel_{unique}"
        task_name = f"harnest.{application_id}.tasks.deliver"
        app = _a2a_task_app(
            deliver,
            application_id=application_id,
            task_name=task_name,
            schedule_in=60,
        )
        with TestClient(app) as client:
            submitted = client.post(
                "/a2a/message:send",
                json=_send_payload("private-value"),
                headers=_a2a_headers(),
            )
            self.assertEqual(submitted.status_code, 200)
            task_id = submitted.json()["task"]["id"]
            _wait_for_a2a_state(client, task_id, "TASK_STATE_WORKING")
            before = asyncio.run(_task_database_evidence(task_name))
            before_continuation = asyncio.run(
                _continuation_database_evidence(task_id)
            )
            cancelled = client.post(
                f"/a2a/tasks/{task_id}:cancel",
                json={"id": task_id},
                headers=_a2a_headers(),
            )
            after = asyncio.run(_task_database_evidence(task_name))
            continuation = asyncio.run(
                _continuation_database_evidence(task_id)
            )

        self.assertEqual(before["payload_status"], "pending")
        self.assertEqual(before["job_status"], "todo")
        self.assertEqual(before_continuation["run_status"], "waiting")
        self.assertEqual(before_continuation["continuation_status"], "pending")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(
            cancelled.json()["status"]["state"], "TASK_STATE_CANCELED"
        )
        self.assertEqual(after["payload_status"], "failed")
        self.assertEqual(after["failure_code"], "task_cancelled")
        self.assertEqual(after["arguments"], "{}")
        self.assertIsNone(after["invocation"])
        self.assertIsNone(after["agent_permissions"])
        self.assertEqual(after["job_status"], "cancelled")
        self.assertEqual(continuation["run_status"], "cancelled")
        self.assertIsNone(continuation["pending_action"])
        self.assertEqual(continuation["continuation_status"], "failed")
        self.assertIn("task_cancelled", continuation["failure"])


async def _wait_for_status(handle, expected):
    """Poll one native row until its worker-side transaction is visible."""

    for _attempt in range(100):
        status = await handle.status()
        if status == expected:
            return status
        await asyncio.sleep(0.05)
    return await handle.status()


if __name__ == "__main__":
    unittest.main()
