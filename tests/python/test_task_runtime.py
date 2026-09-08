from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from harnest.application import CompiledApplication
from harnest.agent_principal import (
    AgentRuntimePrincipal,
    activate_agent_principal,
    active_agent_principal,
    create_agent_principal_binding,
    revoke_agent_principal,
)
from harnest.approval import ApprovalRun
from harnest.checkpoint import MemoryStore, RunScope
from harnest import context, cron
from harnest.context import activate_context, create_agent_context, revoke_context
from harnest.cron import CompiledCron, Cron, CronConflictError, CronNotFoundError
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
from harnest.runtime_contract import InvocationRequest, InvocationResult
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
        self.cron_jobs = {}
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
                "agent_permissions": values["agent_permissions"],
                "trigger": values["trigger"],
                "status": "pending",
                "result": None,
                "failure_code": None,
            }
        elif "DELETE FROM harnest_task_payloads" in query:
            self.payloads.pop(values["payload_id"], None)

    async def execute_query_all_async(self, query, **values):
        if "harnest_cron_jobs" in query:
            return self._cron_query(query, values)
        payload = self.payloads.get(values["payload_id"])
        if "procrastinate_cancel_job_v1" in query:
            return self._cancel_payload(values["payload_id"], payload)
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
                agent_permissions=None,
            )
        elif "status='failed'" in query:
            payload.update(
                status="failed",
                result=None,
                failure_code=values["failure_code"],
                arguments={},
                invocation=None,
                agent_permissions=None,
            )
        return [{"payload_id": values["payload_id"]}]

    def _cron_query(self, query, values):
        """Model the SQL predicates that provide durable cron ownership."""

        if "INSERT INTO harnest_cron_jobs" in query:
            return self._insert_cron(values)
        if query.lstrip().startswith("SELECT"):
            return self._select_crons(query, values)
        if query.lstrip().startswith("UPDATE"):
            return self._update_cron(query, values)
        if query.lstrip().startswith("DELETE"):
            key = (values["application_id"], values["schedule_id"])
            row = self.cron_jobs.get(key)
            if row is None or row["user_id"] != values["user_id"]:
                return []
            del self.cron_jobs[key]
            return [{"schedule_id": values["schedule_id"]}]
        return []

    def _insert_cron(self, values):
        """Apply the application/user/key uniqueness contract."""

        duplicate = next(
            (
                row
                for row in self.cron_jobs.values()
                if row["application_id"] == values["application_id"]
                and row["user_id"] == values["user_id"]
                and row["schedule_key"] == values["schedule_key"]
            ),
            None,
        )
        if duplicate is not None:
            comparable = ("expression", "task_name", "arguments")
            return (
                [dict(duplicate)]
                if all(duplicate[name] == values[name] for name in comparable)
                else []
            )
        row = {
            **values,
            "timezone": "UTC",
            "status": "active",
        }
        self.cron_jobs[(values["application_id"], values["schedule_id"])] = row
        return [dict(row)]

    def _select_crons(self, query, values):
        """Evaluate owner-scoped point reads and ordered list queries."""

        rows = [
            row
            for row in self.cron_jobs.values()
            if row["application_id"] == values["application_id"]
        ]
        rows = self._select_cron_identity(rows, query, values)
        rows = self._select_cron_page(rows, query, values)
        rows.sort(key=lambda row: row["schedule_id"])
        return [dict(row) for row in rows[: values.get("limit")]]

    @staticmethod
    def _select_cron_identity(rows, query, values):
        """Apply point-read selectors independently of list pagination."""

        if "schedule_id=%(schedule_id)s" in query:
            rows = [row for row in rows if row["schedule_id"] == values["schedule_id"]]
        if "schedule_key=%(schedule_key)s" in query:
            rows = [row for row in rows if row["schedule_key"] == values["schedule_key"]]
        return rows

    @staticmethod
    def _select_cron_page(rows, query, values):
        """Apply owner, status, and cursor predicates to one list query."""

        if values.get("user_id") is not None:
            rows = [row for row in rows if row["user_id"] == values["user_id"]]
        if "status='active'" in query:
            rows = [row for row in rows if row["status"] == "active"]
        if "schedule_id > %(after)s" in query:
            rows = [row for row in rows if row["schedule_id"] > values["after"]]
        return rows

    def _update_cron(self, query, values):
        """Apply mutable cron fields, lifecycle guards, and reconciliation."""

        key = (values["application_id"], values["schedule_id"])
        row = self.cron_jobs.get(key)
        if row is None:
            return []
        if values.get("user_id") is not None and row["user_id"] != values["user_id"]:
            return []
        return self._update_cron_row(row, query, values)

    @staticmethod
    def _update_cron_row(row, query, values):
        """Apply one mutable-field or status transition to an owned row."""

        if "SET expression=" in query:
            if row["status"] == "cancelled":
                return []
            row.update(expression=values["expression"], arguments=values["arguments"])
        elif "SET status=%(status)s" in query:
            if row["status"] == "cancelled" and values["status"] != "cancelled":
                return []
            row["status"] = values["status"]
        elif "SET status='paused'" in query and row["status"] == "active":
            row["status"] = "paused"
        else:
            return []
        return [dict(row)]

    def _cancel_payload(self, payload_id, payload):
        """Model the native function and private-row transaction together."""

        if payload is None or payload["status"] != "pending":
            return []
        jobs = [
            job
            for job in self.app.job_manager.jobs.values()
            if job.task_name == payload["task_name"]
            and job.task_kwargs.get("_harnest_payload_id") == payload_id
            and job.status in {"todo", "doing"}
        ]
        if len(jobs) != 1:
            return []
        jobs[0].status = "cancelled"
        payload.update(
            status="failed",
            result=None,
            failure_code="task_cancelled",
            arguments={},
            invocation=None,
            agent_permissions=None,
        )
        return [{"payload_id": payload_id}]

    async def execute_query_one_async(self, query, **values):
        payload = self.payloads[values["payload_id"]]
        task_name = values.get("task_name")
        if task_name is not None and payload["task_name"] != task_name:
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


class _Worker:
    def __init__(self) -> None:
        self.stopped = False
        self._stop = asyncio.Event()

    async def run(self):
        await self._stop.wait()

    def stop(self):
        self.stopped = True
        self._stop.set()


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
        self.periodic_schedules = []
        self.worker_options = None
        self.worker_controller = None

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

    def periodic(self, *, cron, periodic_id):
        def register(native):
            self.periodic_schedules.append((cron, periodic_id, native))
            return native

        return register

    async def open_async(self):
        self.opened = True

    async def close_async(self):
        self.closed = True

    async def check_connection_async(self):
        return self.schema_ready

    def _worker(self, **options):
        self.worker_options = options
        self.worker_controller = _Worker()
        return self.worker_controller


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


class _CronCallingDriver(_Driver):
    """Call public cron APIs as a framework tool would during an invocation."""

    def __init__(self, target) -> None:
        super().__init__()
        self.target = target

    async def invoke(self, request):
        """Create a cron job while the inner framework owns invocation context."""

        active = _cron_invocation(request)
        try:
            with activate_context(active):
                job = await cron.create(
                    key="agent-created",
                    expression="0 9 * * 1-5",
                    task=self.target,
                    arguments={"value": "from-tool"},
                )
        finally:
            revoke_context(active)
        return InvocationResult(
            text=job.id,
            events=(),
            result=job,
            session_id=request.session_id,
            metadata={},
        )

    async def stream(self, request):
        """List cron jobs while the streamed invocation context remains active."""

        active = _cron_invocation(request)
        try:
            with activate_context(active):
                jobs = await cron.list()
                yield {"type": "cron", "count": len(jobs)}
        finally:
            revoke_context(active)


def _cron_invocation(request):
    """Build the managed context normally provided by a framework driver."""

    return create_agent_context(
        framework="langgraph",
        agent_name="reporter",
        invocation_id=request.invocation_id,
        user_id=request.user_id,
        session_id=request.session_id,
        metadata=request.metadata,
        resources={},
    )


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
    async def test_cron_dispatches_private_idempotent_task_occurrences(self):
        observed = []

        async def send_report(value):
            """Send one scheduled report."""

            observed.append(value)

        authored, compiled, application = _compiled(send_report)
        declaration = Cron(
            "0 9 * * 1-5", task=authored, arguments={"value": "private-report"}
        )
        schedule = CompiledCron(
            name="harnest.demo.cron.daily_report",
            source="cron/daily_report.py",
            schedule=declaration.schedule,
            timezone=declaration.timezone,
            task=compiled,
            arguments=declaration.arguments,
        )
        application = replace(application, crons=(schedule,))
        manager = TaskRuntimeManager(application, backend=_BACKEND)
        try:
            with patch.dict(
                "os.environ", {"HARNEST_TASK_DATABASE_URL": "postgresql://tasks"}
            ), patch("harnest.runtime_task._AUDIT") as audit_logger:
                await manager.start()
                self.assertEqual(
                    manager._app.periodic_schedules[0][:2],
                    ("0 9 * * 1-5", schedule.name),
                )
                dispatcher = manager._native_crons[schedule.name]
                await dispatcher.function(1_800_000_000)
                await dispatcher.function(1_800_000_000)
                target_jobs = [
                    job
                    for job in manager._app.job_manager.jobs.values()
                    if job.task_name == compiled.name
                ]
                self.assertEqual(len(target_jobs), 1)
                job = target_jobs[0]
                self.assertEqual(set(job.task_kwargs), {"_harnest_payload_id"})
                payload_id = job.task_kwargs["_harnest_payload_id"]
                payload = manager._app.connector.payloads[payload_id]
                self.assertEqual(payload["arguments"], {"value": "private-report"})
                self.assertEqual(payload["trigger"], "cron")
                await manager._app.tasks[compiled.name].function(
                    _job_context(), payload_id
                )
            self.assertEqual(observed, ["private-report"])
            audit = audit_logger.info
            rendered = repr(audit.mock_calls)
            self.assertNotIn("private-report", rendered)
            self.assertIn("task.cron.enqueue", rendered)
            self.assertIn("task.execute", rendered)
            cron_call = next(
                call
                for call in audit.call_args_list
                if call.args == ("task.cron.enqueue",)
            )
            self.assertEqual(cron_call.kwargs["trigger"], "cron")
            self.assertEqual(cron_call.kwargs["outcome"], "committed")
            self.assertEqual(cron_call.kwargs["schedule"], schedule.name)
        finally:
            await manager.close()

    async def test_local_runtime_can_disable_periodic_registration(self):
        async def send_report():
            """Send one scheduled report."""

        authored, compiled, application = _compiled(send_report)
        schedule = CompiledCron(
            name="harnest.demo.cron.daily_report",
            source="cron/daily_report.py",
            schedule="0 9 * * *",
            timezone="UTC",
            task=compiled,
            arguments={},
        )
        manager = TaskRuntimeManager(
            replace(application, crons=(schedule,)),
            backend=_BACKEND,
            enable_cron=False,
        )
        with patch.dict(
            "os.environ", {"HARNEST_TASK_DATABASE_URL": "postgresql://tasks"}
        ):
            await manager.start()
            handle = await authored.defer()
        self.assertEqual(manager._app.periodic_schedules, [])
        self.assertEqual(await handle.status(), "todo")
        await manager.close()

    async def test_dynamic_cron_is_available_from_agent_invoke_and_stream(self):
        """Expose scheduling to tools for both response transport styles."""

        async def send_report(value):
            """Send one scheduled report."""

        authored, _compiled_task, application = _compiled(send_report)
        manager = TaskRuntimeManager(application, backend=_BACKEND)
        driver = TaskRuntimeDriver(_CronCallingDriver(authored), manager)
        request = InvocationRequest(
            input="schedule it",
            user_id="user-1",
            session_id="session-1",
            invocation_id="inv-cron-tool",
            metadata={},
            state_delta={},
        )
        try:
            with patch.dict(
                "os.environ", {"HARNEST_TASK_DATABASE_URL": "postgresql://tasks"}
            ):
                result = await driver.invoke(request)
                events = [event async for event in driver.stream(request)]
            self.assertEqual(result.result.status, "active")
            self.assertEqual(result.result.arguments, {"value": "from-tool"})
            self.assertEqual(events, [{"type": "cron", "count": 1}])
            stored = next(iter(manager._app.connector.cron_jobs.values()))
            self.assertEqual(stored["user_id"], "user-1")
        finally:
            await driver.close()

    async def test_dynamic_cron_is_available_inside_queued_task_execution(self):
        """Let durable task code manage schedules under its restored owner scope."""

        created = []

        async def send_report(value):
            """Send one scheduled report."""

        authored, compiled, application = _compiled(send_report, name="send_report")

        async def manage_schedule():
            """Create a schedule from a durable task invocation."""

            created.append(
                await cron.create(
                    key="task-created",
                    expression="0 8 * * *",
                    task=authored,
                    arguments={"value": "from-task"},
                )
            )

        manager_authored, manager_compiled, _ = _compiled(
            manage_schedule, name="manage_schedule"
        )
        application = replace(
            application, tasks=(compiled, manager_compiled)
        )
        manager = TaskRuntimeManager(application, backend=_BACKEND)
        active = create_agent_context(
            framework="langgraph",
            agent_name="reporter",
            invocation_id="inv-cron-task",
            user_id="user-1",
            session_id="session-1",
            metadata={},
            resources={},
        )
        try:
            with patch.dict(
                "os.environ", {"HARNEST_TASK_DATABASE_URL": "postgresql://tasks"}
            ):
                await manager.start()
                with activate_context(active):
                    handle = await manager_authored.defer()
                native = manager._app.tasks[manager_compiled.name]
                await native.function(_job_context(), handle._payload_id)
            self.assertEqual(len(created), 1)
            self.assertEqual(created[0].status, "active")
            stored = next(iter(manager._app.connector.cron_jobs.values()))
            self.assertEqual(stored["user_id"], "user-1")
            self.assertEqual(stored["arguments"], {"value": "from-task"})
        finally:
            revoke_context(active)
            await manager.close()

    async def test_dynamic_cron_crud_is_idempotent_and_owner_scoped(self):
        """Enforce key idempotency and ownership across the durable registry API."""

        async def send_report(value):
            """Send one scheduled report."""

        authored, _compiled_task, application = _compiled(send_report)
        manager = TaskRuntimeManager(application, backend=_BACKEND)
        first = create_agent_context(
            framework="langgraph",
            agent_name="reporter",
            invocation_id="inv-first",
            user_id="user-1",
            session_id="session-1",
            metadata={},
            resources={},
        )
        second = create_agent_context(
            framework="langgraph",
            agent_name="reporter",
            invocation_id="inv-second",
            user_id="user-2",
            session_id="session-2",
            metadata={},
            resources={},
        )
        try:
            with patch.dict(
                "os.environ", {"HARNEST_TASK_DATABASE_URL": "postgresql://tasks"}
            ):
                await manager.start()
                with activate_context(first), cron._activate_runtime(
                    manager.cron_runtime
                ):
                    created = await cron.create(
                        key="owned-report",
                        expression="0 9 * * *",
                        task=authored,
                        arguments={"value": "first"},
                    )
                    repeated = await cron.create(
                        key="owned-report",
                        expression="0 9 * * *",
                        task=authored,
                        arguments={"value": "first"},
                    )
                    self.assertEqual(repeated.id, created.id)
                    with self.assertRaises(CronConflictError):
                        await cron.create(
                            key="owned-report",
                            expression="0 10 * * *",
                            task=authored,
                            arguments={"value": "first"},
                        )
                    updated = await created.update(
                        expression="30 9 * * *", arguments={"value": "updated"}
                    )
                    paused = await updated.pause()
                    resumed = await cron.resume(paused.id)
                    self.assertEqual(resumed.status, "active")
                with activate_context(second), cron._activate_runtime(
                    manager.cron_runtime
                ):
                    self.assertIsNone(await cron.get(created.id))
                    self.assertEqual(await cron.list(), ())
                    with self.assertRaises(CronNotFoundError):
                        await cron.cancel(created.id)
                with activate_context(first), cron._activate_runtime(
                    manager.cron_runtime
                ):
                    cancelled = await resumed.cancel()
                    self.assertEqual(cancelled.status, "cancelled")
                    repeated_cancelled = await cron.create(
                        key="owned-report",
                        expression="30 9 * * *",
                        task=authored,
                        arguments={"value": "updated"},
                    )
                    self.assertEqual(repeated_cancelled.status, "cancelled")
                    self.assertTrue(await cron.delete(cancelled.id))
                    self.assertFalse(await cron.delete(cancelled.id))
        finally:
            revoke_context(first)
            revoke_context(second)
            await manager.close()

    async def test_dynamic_cron_dispatch_is_idempotent_and_restores_owner(self):
        """Queue each due occurrence once and execute it as the schedule owner."""

        observed = []

        async def send_report(value):
            """Record the private value and owner restored by the worker."""

            observed.append((value, context.user_id))

        authored, compiled, application = _compiled(send_report)
        manager = TaskRuntimeManager(application, backend=_BACKEND)
        active = create_agent_context(
            framework="langgraph",
            agent_name="reporter",
            invocation_id="inv-cron-create",
            user_id="user-1",
            session_id="session-1",
            metadata={},
            resources={},
        )
        timestamp = 1_788_771_600  # 2026-09-07 09:00:00 UTC
        try:
            with patch.dict(
                "os.environ", {"HARNEST_TASK_DATABASE_URL": "postgresql://tasks"}
            ):
                await manager.start()
                with activate_context(active), cron._activate_runtime(
                    manager.cron_runtime
                ):
                    job = await cron.create(
                        key="weekday-report",
                        expression="0 9 * * 1-5",
                        task=authored,
                        arguments={"value": "private"},
                    )
                await manager._dynamic_cron_dispatcher.function(timestamp)
                await manager._dynamic_cron_dispatcher.function(timestamp)
                matching = [
                    queued
                    for queued in manager._app.job_manager.jobs.values()
                    if queued.task_name == compiled.name
                ]
                self.assertEqual(len(matching), 1)
                payload_id = matching[0].task_kwargs["_harnest_payload_id"]
                payload = manager._app.connector.payloads[payload_id]
                self.assertEqual(payload["trigger"], "cron")
                self.assertEqual(payload["arguments"], {"value": "private"})
                self.assertEqual(payload["invocation"]["user_id"], "user-1")
                self.assertNotIn("private", matching[0].task_kwargs)
                await manager._app.tasks[compiled.name].function(
                    _job_context(), payload_id
                )
                with activate_context(active), cron._activate_runtime(
                    manager.cron_runtime
                ):
                    cancelled = await cron.cancel(job.id)
                    with self.assertRaises(CronConflictError):
                        await cancelled.resume()
                    with self.assertRaises(CronConflictError):
                        await cancelled.update(expression="0 10 * * *")
            self.assertEqual(observed, [("private", "user-1")])
        finally:
            revoke_context(active)
            await manager.close()

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

    async def test_deferred_task_restores_principal_grants_for_nested_work(self):
        observed = []

        async def inspect_principal():
            """Record the worker-scoped principal reconstructed from its payload."""

            observed.append(active_agent_principal())

        authored, compiled, application = _compiled(inspect_principal)
        manager = TaskRuntimeManager(application, backend=_BACKEND)
        active = create_agent_context(
            framework="langgraph",
            agent_name="reporter",
            invocation_id="inv-principal",
            user_id="user-1",
            session_id="session-1",
            metadata={},
            resources={},
        )
        parent = AgentRuntimePrincipal.create(
            permissions={"reports.read", "reports.compare"}
        )
        binding = create_agent_principal_binding(parent)
        try:
            with patch.dict(
                "os.environ", {"HARNEST_TASK_DATABASE_URL": "postgresql://tasks"}
            ):
                await manager.start()
                with activate_context(active), activate_agent_principal(binding):
                    handle = await authored.defer()
                payload = manager._app.connector.payloads[handle._payload_id]
                self.assertEqual(
                    payload["agent_permissions"],
                    ["reports.compare", "reports.read"],
                )
                await manager._app.tasks[compiled.name].function(
                    _job_context(), handle._payload_id
                )
            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0].permissions, parent.permissions)
            self.assertNotEqual(observed[0].id, parent.id)
            self.assertIsNone(active_agent_principal())
        finally:
            revoke_agent_principal(binding)
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
                TaskUnavailableError, r"@tool\(durable=True\)"
            ):
                await handle.result()
        finally:
            await manager.close()
            await continuations.close()
            await store.close()

    async def test_worker_completion_reconciles_after_callback_outage(self):
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
                port = manager._application_continuations
                manager._application_continuations = None
                await native.function(_job_context(), handle._payload_id)
                before = await continuations.provider("harnest.task").lookup(
                    handle._payload_id
                )
                self.assertIsNotNone(before)
                assert before is not None
                self.assertEqual(before.record.status, "pending")
                manager._application_continuations = port
                await manager.reconcile_continuations()
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

    async def test_transport_cancellation_stops_awaited_task_and_durable_run(self):
        async def send_report(value):
            """Return one deferred report."""

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
            invocation_id="inv-cancel",
            metadata={},
            state_delta={},
        )
        run = ApprovalRun(
            id=request.invocation_id,
            user_id=request.user_id,
            session_id=request.session_id,
            call_id=request.invocation_id,
        )
        artifact = ResumeArtifact("adk", "native-cancel", "call-cancel", "report")
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
                handle = await authored.defer("private")
                with (
                    continuations.execution(run, request),
                    native_durable_call(artifact),
                    self.assertRaises(NativeDurableSuspended),
                ):
                    await handle.result()
                await continuations.arm(
                    response_id=request.invocation_id,
                    user_id=request.user_id,
                    session_id=request.session_id,
                )
                self.assertFalse(
                    await continuations.cancel_task_wait(
                        response_id=request.invocation_id,
                        user_id="another-user",
                        session_id=request.session_id,
                    )
                )
                self.assertEqual(
                    manager._app.job_manager.jobs[int(handle.id)].status, "todo"
                )
                cancelled = await continuations.cancel_task_wait(
                    response_id=request.invocation_id,
                    user_id=request.user_id,
                    session_id=request.session_id,
                )
            self.assertTrue(cancelled)
            self.assertEqual(
                manager._app.job_manager.jobs[int(handle.id)].status, "cancelled"
            )
            self.assertEqual(
                manager._app.connector.payloads[handle._payload_id]["failure_code"],
                "task_cancelled",
            )
            durable = await store.get_run(
                scope=RunScope(
                    "demo", request.user_id, request.session_id, request.invocation_id
                )
            )
            self.assertIsNotNone(durable)
            assert durable is not None
            self.assertEqual(durable.status, "cancelled")
        finally:
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
                        agent_permissions=None,
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
        reconciliation_states = []

        async def reconcile_after_start():
            """Re-enter startup as a callback-driven resume would."""

            reconciliation_states.append(driver._state)
            await driver.start()

        manager.reconcile_continuations = reconcile_after_start
        with patch.dict(
            "os.environ", {"HARNEST_TASK_DATABASE_URL": "postgresql://tasks"}
        ):
            await driver.start()
            await asyncio.sleep(0)
        self.assertTrue(inner.started)
        self.assertTrue(manager._app.opened)
        self.assertEqual(reconciliation_states, ["started"])
        self.assertEqual(
            manager._app.worker_options,
            {
                "install_signal_handlers": False,
                "shutdown_graceful_timeout": 10,
            },
        )
        app = manager._app
        await driver.close()
        self.assertTrue(app.closed)
        self.assertTrue(app.worker_controller.stopped)
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
