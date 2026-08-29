from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from harnest.application import CompiledApplication
from harnest.approval import ApprovalRun
from harnest.checkpoint import MemoryStore
from harnest.context import activate_context, create_agent_context, revoke_context
from harnest.durable import (
    NativeDurableSuspended,
    ResumeArtifact,
    native_durable_call,
)
from harnest.external_continuation import ExternalContinuationRuntime
from harnest.runtime_task import (
    TaskExecutionError,
    TaskRuntimeDriver,
    TaskRuntimeError,
    TaskRuntimeManager,
)
from harnest.runtime import _runtime_driver
from harnest.runtime_contract import InvocationRequest
from harnest.task import (
    CompiledTask,
    TaskUnavailableError,
    registration_for,
    task,
)


class _AlreadyEnqueued(Exception):
    pass


class _Status:
    def __init__(self, value: str) -> None:
        self.value = value


class _Job:
    def __init__(self, job_id, task_name, task_kwargs, queueing_lock) -> None:
        self.id = job_id
        self.task_name = task_name
        self.task_kwargs = task_kwargs
        self.queueing_lock = queueing_lock
        self.status = "todo"


class _JobManager:
    def __init__(self) -> None:
        self.jobs = {}
        self.next_id = 1

    async def defer(self, native, options, task_kwargs):
        lock = options.get("queueing_lock")
        if lock is not None:
            for job in self.jobs.values():
                if job.queueing_lock == lock and job.status == "todo":
                    raise _AlreadyEnqueued
        job = _Job(self.next_id, native.name, task_kwargs, lock)
        self.jobs[job.id] = job
        self.next_id += 1
        return job.id

    async def list_jobs_async(self, *, task, queueing_lock):
        return [
            job
            for job in self.jobs.values()
            if job.task_name == task and job.queueing_lock == queueing_lock
        ]

    async def get_job_status_async(self, job_id):
        return _Status(self.jobs[job_id].status)

    async def cancel_job_by_id_async(self, job_id, abort=False):
        job = self.jobs.get(job_id)
        if job is None or job.status not in {"todo", "doing"}:
            return False
        job.status = "cancelled"
        return abort


class _Connector:
    def __init__(self, *, conninfo) -> None:
        self.conninfo = conninfo
        self.payloads = {}
        self.pool = self
        self.app = None

    @asynccontextmanager
    async def connection(self):
        yield self

    async def execute_query_all_async_with_connection(
        self, _connection, query, **values
    ):
        if "FROM procrastinate_jobs" in query:
            return [
                {"id": job.id, "args": job.task_kwargs}
                for job in self.app.job_manager.jobs.values()
                if job.task_name == values["task_name"]
                and job.queueing_lock == values["queueing_lock"]
                and job.task_kwargs.get("_harnest_payload_id")
                == values["payload_id"]
            ][:2]
        if values["payload_id"] in self.payloads:
            return []
        await self.execute_query_async(query, **values)
        return [{"payload_id": values["payload_id"]}]

    async def execute_query_async(self, query, **values):
        if "INSERT INTO harnest_task_payloads" in query:
            self.payloads[values["payload_id"]] = {
                "task_name": values["task_name"],
                "arguments": values["arguments"],
                "invocation": values["invocation"],
                "status": "pending",
                "result": None,
                "failure_code": None,
            }
        elif "DELETE FROM harnest_task_payloads" in query:
            self.payloads.pop(values["payload_id"], None)

    async def execute_query_all_async(self, query, **values):
        payload = self.payloads.get(values["payload_id"])
        if (
            payload is None
            or payload["task_name"] != values["task_name"]
            or payload["status"] != "pending"
        ):
            return []
        if "status='completed'" in query:
            payload.update(
                status="completed",
                result=values["result"],
                failure_code=None,
                arguments={},
                invocation=None,
            )
        elif "status='failed'" in query:
            payload.update(
                status="failed",
                result=None,
                failure_code=values["failure_code"],
                arguments={},
                invocation=None,
            )
        return [{"payload_id": values["payload_id"]}]

    async def execute_query_one_async(self, query, **values):
        payload = self.payloads[values["payload_id"]]
        if payload["task_name"] != values["task_name"]:
            raise LookupError
        return payload


class _SchemaManager:
    def __init__(self, app) -> None:
        self.app = app

    async def apply_schema_async(self):
        self.app.schema_ready = True


class _Configured:
    def __init__(self, native, options) -> None:
        self.native = native
        self.options = options

    async def defer_async(self, **task_kwargs):
        return await self.native.app.job_manager.defer(
            self.native, self.options, task_kwargs
        )


class _NativeTask:
    def __init__(self, app, function, *, name, queue, retry, pass_context) -> None:
        self.app = app
        self.function = function
        self.name = name
        self.queue = queue
        self.retry = retry
        self.pass_context = pass_context
        self.configurations = []

    def configure(self, **options):
        self.configurations.append(options)
        return _Configured(self, options)


class _App:
    def __init__(self, *, connector) -> None:
        self.connector = connector
        self.connector.app = self
        self.job_manager = _JobManager()
        self.schema_manager = _SchemaManager(self)
        self.schema_ready = False
        self.opened = False
        self.closed = False
        self.tasks = {}

    def task(self, *, name, queue, retry, pass_context=False):
        def register(function):
            native = _NativeTask(
                self,
                function,
                name=name,
                queue=queue,
                retry=retry,
                pass_context=pass_context,
            )
            self.tasks[name] = native
            return native

        return register

    async def open_async(self):
        self.opened = True

    async def close_async(self):
        self.closed = True

    async def check_connection_async(self):
        return self.schema_ready

    async def run_worker_async(self, **_options):
        await asyncio.Event().wait()


_BACKEND = SimpleNamespace(
    App=_App,
    PsycopgConnector=_Connector,
    exceptions=SimpleNamespace(AlreadyEnqueued=_AlreadyEnqueued),
)


def _job_context(*, attempts=0):
    """Build only the native retry metadata consumed by the task wrapper."""

    return SimpleNamespace(job=SimpleNamespace(attempts=attempts))


class _Driver:
    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self.info = SimpleNamespace()

    async def start(self):
        self.started = True

    async def close(self):
        self.closed = True


def _compiled(function, *, name="send_report", retries=3):
    decorated = task(queue="reports", max_retries=retries)(function)
    definition = registration_for(decorated)
    assert definition is not None
    compiled = CompiledTask(
        name=f"harnest.demo.tasks.{name}",
        source=f"tasks/{name}.py",
        definition=definition,
        authored=decorated,
    )
    application = CompiledApplication(
        name="demo",
        framework="langgraph",
        mode="managed",
        target=object(),
        tasks=(compiled,),
    )
    return decorated, compiled, application


class TaskRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_defer_keeps_payload_out_of_native_job_and_restores_identity(self):
        observed = []

        async def send_report(value):
            """Record one deferred report."""

            from harnest import context

            observed.append((value, context.user_id, context.metadata["kind"]))
            return "must-not-enter-native-result"

        authored, compiled, application = _compiled(send_report)
        manager = TaskRuntimeManager(application, backend=_BACKEND)
        active = create_agent_context(
            framework="langgraph",
            agent_name="reporter",
            invocation_id="inv-1",
            user_id="user-1",
            session_id="session-1",
            metadata={"kind": "daily"},
            resources={},
        )
        try:
            with patch.dict(
                "os.environ", {"HARNEST_TASK_DATABASE_URL": "postgresql://tasks"}
            ):
                await manager.start()
                with activate_context(active), self.assertLogs(
                    "harnest.agent.task.audit", level="INFO"
                ) as audit:
                    handle = await authored.defer(
                        "private-report", schedule_in=2.5, idempotency_key="tenant-key"
                    )
                native = manager._app.tasks[compiled.name]
                native_job = manager._app.job_manager.jobs[int(handle.id)]
                self.assertEqual(
                    native_job.task_kwargs, {"_harnest_payload_id": handle._payload_id}
                )
                self.assertEqual(
                    native.configurations[-1]["schedule_in"], {"seconds": 2.5}
                )
                self.assertNotIn("tenant-key", repr(native.configurations))
                self.assertNotIn("private-report", " ".join(audit.output))
                await native.function(_job_context(), handle._payload_id)
                self.assertEqual(observed, [("private-report", "user-1", "daily")])
                payload = manager._app.connector.payloads[handle._payload_id]
                self.assertEqual(payload["status"], "completed")
                self.assertEqual(payload["arguments"], {})
                self.assertEqual(await handle.result(), "must-not-enter-native-result")
                cancel_handle = await authored.defer("cancel-report")
                self.assertEqual(await cancel_handle.status(), "todo")
                self.assertTrue(await cancel_handle.cancel())
                with self.assertRaisesRegex(TaskExecutionError, "task_cancelled"):
                    await cancel_handle.result()
        finally:
            revoke_context(active)
            await manager.close()

    async def test_idempotency_returns_existing_job_without_duplicate_payload(self):
        async def send_report(value):
            """Send one report."""

        authored, compiled, application = _compiled(send_report)
        manager = TaskRuntimeManager(application, backend=_BACKEND)
        with patch.dict(
            "os.environ", {"HARNEST_TASK_DATABASE_URL": "postgresql://tasks"}
        ):
            await manager.start()
            first = await authored.defer("one", idempotency_key="same")
            second = await authored.defer("two", idempotency_key="same")
        self.assertEqual(first.id, second.id)
        self.assertEqual(first._payload_id, second._payload_id)
        self.assertEqual(len(manager._app.connector.payloads), 1)
        await manager.close()

    async def test_native_durable_call_derives_replay_safe_queue_idempotency(self):
        async def send_report(value):
            """Send one report."""

        authored, compiled, application = _compiled(send_report)
        manager = TaskRuntimeManager(application, backend=_BACKEND)
        active = create_agent_context(
            framework="langgraph",
            agent_name="reporter",
            invocation_id="inv-native",
            user_id="user-1",
            session_id="session-1",
            metadata={},
            resources={},
        )
        artifact = ResumeArtifact(
            "langgraph", "native-thread", "tool-call", "create_report"
        )
        try:
            with patch.dict(
                "os.environ", {"HARNEST_TASK_DATABASE_URL": "postgresql://tasks"}
            ):
                await manager.start()
                with activate_context(active), native_durable_call(artifact):
                    first = await authored.defer("same")
                native = manager._app.tasks[compiled.name]
                await native.function(_job_context(), first._payload_id)
                manager._app.job_manager.jobs[int(first.id)].status = "succeeded"
                with activate_context(active), native_durable_call(artifact):
                    replay = await authored.defer("same")
                    distinct = await authored.defer("different")
            self.assertEqual(first.id, replay.id)
            self.assertEqual(first._payload_id, replay._payload_id)
            self.assertNotEqual(first.id, distinct.id)
            self.assertNotIn(
                "same", manager._app.job_manager.jobs[int(first.id)].queueing_lock
            )
        finally:
            revoke_context(active)
            await manager.close()

    async def test_unfinished_result_requires_native_durable_tool(self):
        async def send_report(value):
            """Send one report."""

            return {"report": value}

        authored, _compiled_task, application = _compiled(send_report)
        store = MemoryStore()
        await store.start()
        continuations = ExternalContinuationRuntime(store, application_id="demo")
        manager = TaskRuntimeManager(
            application,
            backend=_BACKEND,
            continuation_runtime=continuations,
        )
        request = InvocationRequest(
            input="report",
            user_id="user-1",
            session_id="session-1",
            invocation_id="inv-result",
            metadata={},
            state_delta={},
        )
        run = ApprovalRun(
            id=request.invocation_id,
            user_id=request.user_id,
            session_id=request.session_id,
            call_id=request.invocation_id,
        )
        try:
            with patch.dict(
                "os.environ", {"HARNEST_TASK_DATABASE_URL": "postgresql://tasks"}
            ):
                await manager.start()
                handle = await authored.defer("private")
            with continuations.execution(run, request), self.assertRaisesRegex(
                TaskUnavailableError, "@tool\(durable=True\)"
            ):
                await handle.result()
        finally:
            await manager.close()
            await continuations.close()
            await store.close()

    async def test_worker_completion_resolves_registered_native_wait(self):
        async def send_report(value):
            """Send one report."""

            return {"report": value}

        authored, compiled, application = _compiled(send_report)
        store = MemoryStore()
        await store.start()
        continuations = ExternalContinuationRuntime(store, application_id="demo")
        manager = TaskRuntimeManager(
            application,
            backend=_BACKEND,
            continuation_runtime=continuations,
        )
        active = create_agent_context(
            framework="adk",
            agent_name="reporter",
            invocation_id="inv-result",
            user_id="user-1",
            session_id="session-1",
            metadata={},
            resources={},
        )
        request = InvocationRequest(
            input="report",
            user_id="user-1",
            session_id="session-1",
            invocation_id="inv-result",
            metadata={},
            state_delta={},
        )
        run = ApprovalRun(
            id=request.invocation_id,
            user_id=request.user_id,
            session_id=request.session_id,
            call_id=request.invocation_id,
        )
        artifact = ResumeArtifact("adk", "native-invocation", "call-1", "report")
        try:
            await store.begin_run(
                application_id="demo",
                user_id=request.user_id,
                session_id=request.session_id,
                run_id=request.invocation_id,
                framework="adk",
            )
            with patch.dict(
                "os.environ", {"HARNEST_TASK_DATABASE_URL": "postgresql://tasks"}
            ):
                await manager.start()
                with activate_context(active):
                    handle = await authored.defer("private")
                with (
                    continuations.execution(run, request),
                    native_durable_call(artifact),
                    self.assertRaises(NativeDurableSuspended),
                ):
                    await handle.result()
                native = manager._app.tasks[compiled.name]
                await native.function(_job_context(), handle._payload_id)
            pending = await continuations.provider("harnest.task").lookup(
                handle._payload_id
            )
            self.assertIsNotNone(pending)
            assert pending is not None
            self.assertEqual(pending.record.status, "completed")
            self.assertEqual(
                manager._app.connector.payloads[handle._payload_id]["result"],
                {"value": {"report": "private"}},
            )
        finally:
            revoke_context(active)
            await manager.close()
            await continuations.close()
            await store.close()

    async def test_result_rechecks_work_that_finishes_during_registration(self):
        async def send_report(value):
            """Send one report."""

            return value

        authored, _compiled_task, application = _compiled(send_report)
        store = MemoryStore()
        await store.start()
        continuations = ExternalContinuationRuntime(store, application_id="demo")
        manager = TaskRuntimeManager(
            application,
            backend=_BACKEND,
            continuation_runtime=continuations,
        )
        request = InvocationRequest(
            input="report",
            user_id="user-1",
            session_id="session-1",
            invocation_id="inv-race",
            metadata={},
            state_delta={},
        )
        run = ApprovalRun(
            id=request.invocation_id,
            user_id=request.user_id,
            session_id=request.session_id,
            call_id=request.invocation_id,
        )
        artifact = ResumeArtifact("adk", "native-race", "call-race", "report")
        try:
            await store.begin_run(
                application_id="demo",
                user_id=request.user_id,
                session_id=request.session_id,
                run_id=request.invocation_id,
                framework="adk",
            )
            with patch.dict(
                "os.environ", {"HARNEST_TASK_DATABASE_URL": "postgresql://tasks"}
            ):
                await manager.start()
                handle = await authored.defer("ready")
                original = manager._invocation_continuations

                async def finish_during_suspend(*args, **kwargs):
                    """Persist work after the wait row exists but before it returns."""

                    suspended = await original.suspend(*args, **kwargs)
                    manager._app.connector.payloads[handle._payload_id].update(
                        status="completed",
                        result={"value": "ready"},
                        arguments={},
                        invocation=None,
                    )
                    return suspended

                manager._invocation_continuations = SimpleNamespace(
                    suspend=finish_during_suspend
                )
                with (
                    continuations.execution(run, request),
                    native_durable_call(artifact),
                    self.assertRaises(NativeDurableSuspended),
                ):
                    await handle.result()
            pending = await continuations.provider("harnest.task").lookup(
                handle._payload_id
            )
            self.assertIsNotNone(pending)
            assert pending is not None
            self.assertEqual(pending.record.status, "completed")
        finally:
            await manager.close()
            await continuations.close()
            await store.close()

    async def test_authored_failure_is_sanitized_and_payload_is_retained_for_retry(
        self,
    ):
        async def send_report(secret):
            """Fail one report without logging its data."""

            raise ValueError(secret)

        authored, compiled, application = _compiled(send_report, retries=4)
        manager = TaskRuntimeManager(application, backend=_BACKEND)
        with patch.dict(
            "os.environ", {"HARNEST_TASK_DATABASE_URL": "postgresql://tasks"}
        ):
            await manager.start()
            handle = await authored.defer("credential-value")
            native = manager._app.tasks[compiled.name]
            with self.assertRaisesRegex(TaskExecutionError, "ValueError") as raised:
                await native.function(
                    _job_context(attempts=compiled.max_retries), handle._payload_id
                )
        self.assertNotIn("credential-value", str(raised.exception))
        self.assertIn(handle._payload_id, manager._app.connector.payloads)
        self.assertEqual(
            manager._app.connector.payloads[handle._payload_id]["failure_code"],
            "task_failed",
        )
        self.assertEqual(native.retry, 4)
        await manager.close()

    async def test_retryable_failure_keeps_result_pending_until_final_attempt(self):
        async def send_report(secret):
            """Fail one report without logging its data."""

            raise ValueError(secret)

        authored, compiled, application = _compiled(send_report, retries=2)
        manager = TaskRuntimeManager(application, backend=_BACKEND)
        with patch.dict(
            "os.environ", {"HARNEST_TASK_DATABASE_URL": "postgresql://tasks"}
        ):
            await manager.start()
            handle = await authored.defer("credential-value")
            native = manager._app.tasks[compiled.name]
            with self.assertRaises(TaskExecutionError):
                await native.function(_job_context(attempts=0), handle._payload_id)
        payload = manager._app.connector.payloads[handle._payload_id]
        self.assertEqual(payload["status"], "pending")
        self.assertEqual(payload["arguments"], {"secret": "credential-value"})
        await manager.close()

    async def test_manager_imports_backend_only_on_start_and_requires_postgres(self):
        async def send_report():
            """Send one report."""

        _authored, _compiled_task, application = _compiled(send_report)
        manager = TaskRuntimeManager(application)
        with patch("harnest.runtime_task.importlib.import_module") as importer:
            self.assertEqual(manager._state, "new")
            importer.assert_not_called()
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(TaskRuntimeError, "require.*PostgresStore"):
                await TaskRuntimeManager(application, backend=_BACKEND).start()

    async def test_driver_orders_task_runtime_inside_capability_lifetime(self):
        async def send_report():
            """Send one report."""

        _authored, _compiled_task, application = _compiled(send_report)
        inner = _Driver()
        manager = TaskRuntimeManager(application, backend=_BACKEND)
        driver = TaskRuntimeDriver(inner, manager)
        with patch.dict(
            "os.environ", {"HARNEST_TASK_DATABASE_URL": "postgresql://tasks"}
        ):
            await driver.start()
        self.assertTrue(inner.started)
        self.assertTrue(manager._app.opened)
        app = manager._app
        await driver.close()
        self.assertTrue(app.closed)
        self.assertTrue(inner.closed)

    async def test_runtime_selection_wraps_only_applications_with_tasks(self):
        async def send_report():
            """Send one report."""

        _authored, _compiled_task, application = _compiled(send_report)
        inner = _Driver()
        with patch(
            "harnest.runtime._langgraph_runtime_driver", return_value=inner
        ), patch("harnest.runtime._wrap_runtime_driver", return_value=inner):
            selected = _runtime_driver(application)
        self.assertIsInstance(selected, TaskRuntimeDriver)
        with patch.dict(
            "os.environ", {"HARNEST_TASK_DATABASE_URL": "postgresql://tasks"}
        ), patch("harnest.runtime_task._load_procrastinate", return_value=_BACKEND):
            await selected.start()
        await selected.close()


class TaskAuthoringTests(unittest.TestCase):
    def test_schedule_and_idempotency_controls_are_validated_before_runtime(self):
        @task
        def work(value):
            """Perform work."""

        async def exercise():
            with self.assertRaisesRegex(ValueError, "schedule_in"):
                await work.defer("x", schedule_in=float("nan"))
            with self.assertRaisesRegex(ValueError, "idempotency_key"):
                await work.defer("x", idempotency_key="")

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
