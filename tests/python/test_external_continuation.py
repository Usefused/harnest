from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest

from harnest.approval import ApprovalRun
from harnest.agent import AgentRuntimePrincipal
from harnest.checkpoint import MemoryStore, RunScope
from harnest.continuation import ContinuationConflictError
from harnest.durable import (
    NativeDurableSuspended,
    NativeResumeInput,
    ResumeArtifact,
    native_durable_call,
)
from harnest.external_continuation import (
    ExternalContinuationRuntime,
    ExternalContinuationUnavailableError,
)
from harnest.runtime_contract import InvocationRequest
from harnest.runtime_contract import SessionMessage, SessionRecord
from harnest.runtime_invocation import InvocationCoordinator


def _request(
    invocation_id: str,
    *,
    session_id: str = "session-1",
    agent_principal: AgentRuntimePrincipal | None = None,
) -> InvocationRequest:
    """Build one fully scoped invocation without private provider content."""

    return InvocationRequest(
        input="run external work",
        user_id="user-1",
        session_id=session_id,
        invocation_id=invocation_id,
        metadata={},
        state_delta={},
        agent_principal=agent_principal,
    )


def _run(request: InvocationRequest) -> ApprovalRun:
    """Create the process-local response channel paired with a durable run."""

    return ApprovalRun(
        id=request.invocation_id,
        user_id=request.user_id,
        session_id=request.session_id,
        call_id=request.invocation_id,
    )


def _artifact(invocation_id: str) -> ResumeArtifact:
    """Use ADK identity because its test suspension needs no native graph."""

    return ResumeArtifact(
        framework="adk",
        native_invocation_id=f"native-{invocation_id}",
        tool_call_id=f"call-{invocation_id}",
        tool_name="external_report",
    )


class _ResumeDriver:
    """Capture callback-driven native resume requests without a model backend."""

    def __init__(self) -> None:
        self.requests: list[InvocationRequest] = []
        self.info = SimpleNamespace()

    async def invoke(self, request: InvocationRequest) -> object:
        """Return one opaque result after recording the private resume input."""

        self.requests.append(request)
        return {"resumed": True}


class _PollingDriver:
    """Expose a committed portable transcript to a stateless HTTP replica."""

    def __init__(self, session: SessionRecord, messages: list[SessionMessage]) -> None:
        self.session = session
        self.messages = messages

    async def get_session(
        self, *, session_id: str, user_id: str
    ) -> SessionRecord | None:
        """Authorize the same complete ownership pair as production drivers."""

        if self.session.id != session_id or self.session.user_id != user_id:
            return None
        return self.session

    async def get_session_messages(
        self, *, session_id: str, user_id: str
    ) -> list[SessionMessage] | None:
        """Return messages only for the owned durable session."""

        if self.session.id != session_id or self.session.user_id != user_id:
            return None
        return list(self.messages)


async def _begin(store: MemoryStore, request: InvocationRequest) -> None:
    """Start the portable run a managed framework driver normally owns."""

    await store.begin_run(
        application_id="consumer",
        user_id=request.user_id,
        session_id=request.session_id,
        run_id=request.invocation_id,
        framework="adk",
    )


def _validate(value: object) -> object:
    """Accept the JSON-shaped test result under a stable replica registry key."""

    return value


class ExternalContinuationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        """Give each coordination test isolated durable and local state."""

        self.store = MemoryStore()
        await self.store.start()
        self.runtime = ExternalContinuationRuntime(
            self.store, application_id="consumer", max_responses=1
        )
        self.driver = _ResumeDriver()
        self.runtime.bind_driver(self.driver)
        self.port = self.runtime.invocation_port("hatchet")

    async def asyncTearDown(self) -> None:
        """Release local response state without cancelling provider work."""

        await self.runtime.close()
        await self.store.close()

    async def _suspend(self, request: InvocationRequest) -> ApprovalRun:
        """Register a wait under the durable tool identity used by the framework."""

        run = _run(request)
        with self.runtime.execution(run, request), native_durable_call(
            _artifact(request.invocation_id)
        ):
            handle = await self.port.suspend(
                f"provider-{request.invocation_id}",
                capability="hatchet.run",
                schema_id="report/v1",
                validate=_validate,
            )
            with self.assertRaises(NativeDurableSuspended):
                await handle.result()
        return run

    async def test_completion_before_checkpoint_arms_and_resumes_once(self):
        """Close the callback-before-checkpoint race without a Python Future."""

        request = _request("run-success")
        await _begin(self.store, request)
        run = await self._suspend(request)
        completed = await self.runtime.application_port("hatchet").complete(
            "provider-run-success", {"report": "ready"}
        )

        self.assertFalse(completed.ready)
        self.assertEqual(self.driver.requests, [])
        claimed = await self.runtime.arm(
            response_id=request.invocation_id,
            user_id=request.user_id,
            session_id=request.session_id,
        )

        self.assertEqual(claimed.status, "claimed")
        self.assertEqual(len(self.driver.requests), 1)
        resume = self.driver.requests[0].input
        self.assertIsInstance(resume, NativeResumeInput)
        self.assertEqual(resume.value, {"report": "ready"})
        self.assertEqual((await run.notifications.get())[0], "external_continuation")
        self.assertEqual((await run.notifications.get())[0], "result")
        with self.assertRaises(ContinuationConflictError):
            await self.runtime.application_port("hatchet").complete(
                "provider-run-success", {"report": "duplicate"}
            )

    async def test_checkpoint_before_completion_resumes_on_callback_replica(self):
        """Let any replica claim work after the initial process has armed it."""

        request = _request("run-later")
        await _begin(self.store, request)
        await self._suspend(request)
        armed = await self.runtime.arm(
            response_id=request.invocation_id,
            user_id=request.user_id,
            session_id=request.session_id,
        )
        self.assertTrue(armed.ready)
        self.assertEqual(self.driver.requests, [])

        replica = ExternalContinuationRuntime(self.store, application_id="consumer")
        replica_driver = _ResumeDriver()
        replica.bind_driver(replica_driver)
        replica.application_port("hatchet").register_schema("report/v1", _validate)
        try:
            pending_boundary = await replica.response_boundary(
                response_id="run-later",
                user_id="user-1",
                session_id="session-1",
            )
            claimed = await replica.application_port("hatchet").complete(
                "provider-run-later", {"report": "cross-replica"}
            )
            await self.store.transition(
                scope=RunScope("consumer", "user-1", "session-1", "run-later"),
                expected_status="running",
                status="completed",
            )
            origin_boundary = await self.runtime.response_boundary(
                response_id="run-later",
                user_id="user-1",
                session_id="session-1",
            )
            replica_boundary = await replica.response_boundary(
                response_id="run-later",
                user_id="user-1",
                session_id="session-1",
            )
        finally:
            await replica.close()

        self.assertEqual(claimed.status, "claimed")
        self.assertEqual(pending_boundary[0], "external_continuation")
        self.assertEqual(pending_boundary[1].id, armed.continuation_id)
        self.assertEqual(pending_boundary[1].capability, "hatchet.run")
        self.assertEqual(len(replica_driver.requests), 1)
        self.assertEqual(
            replica_driver.requests[0].input.value,
            {"report": "cross-replica"},
        )
        self.assertEqual(origin_boundary[0], "durable_terminal")
        self.assertEqual(replica_boundary[0], "result")
        with self.assertRaises(KeyError):
            await self.runtime.response_boundary(
                response_id="run-later",
                user_id="other-user",
                session_id="session-1",
            )

    async def test_stateless_replica_reconstructs_terminal_http_response(self):
        """Return the committed assistant turn without a process-local waiter."""

        request = _request("run-poll")
        await _begin(self.store, request)
        await self._suspend(request)
        await self.runtime.arm(
            response_id=request.invocation_id,
            user_id=request.user_id,
            session_id=request.session_id,
        )
        await self.runtime.application_port("hatchet").complete(
            "provider-run-poll", {"report": "ready"}
        )
        terminal = await self.store.transition(
            scope=RunScope("consumer", "user-1", "session-1", "run-poll"),
            expected_status="running",
            status="completed",
        )
        session = SessionRecord(
            id="session-1",
            user_id="user-1",
            state={},
            updated_at=terminal.created_at,
        )
        driver = _PollingDriver(
            session,
            [
                SessionMessage(
                    id="message-1",
                    role="assistant",
                    content="external report is ready",
                    created_at=terminal.created_at,
                )
            ],
        )
        replica = ExternalContinuationRuntime(
            self.store, application_id="consumer"
        )
        coordinator = InvocationCoordinator(
            driver=driver,
            approvals=None,
            client_tools=None,
            assets=None,
            asset_stores={},
            semaphore=asyncio.Semaphore(1),
            request_timeout=1,
            max_request_bytes=1024,
            external_continuations=replica,
        )
        try:
            payload = await coordinator.poll_json(
                response_id="run-poll",
                user_id="user-1",
                session_id="session-1",
            )
            # Once reconstructed, this replica must not drift to a later turn.
            driver.session = SessionRecord(
                id="session-1",
                user_id="user-1",
                state={},
                updated_at="9999-01-01T00:00:00+00:00",
            )
            driver.messages = [
                SessionMessage(
                    id="message-2",
                    role="assistant",
                    content="a later unrelated turn",
                )
            ]
            repeated = await coordinator.poll_json(
                response_id="run-poll",
                user_id="user-1",
                session_id="session-1",
            )
        finally:
            await replica.close()

        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["outputText"], "external report is ready")
        self.assertEqual(repeated, payload)

    async def test_capacity_rejects_before_second_run_becomes_waiting(self):
        """Never persist work the bounded HTTP response surface cannot expose."""

        first = _request("run-one")
        second = _request("run-two", session_id="session-2")
        await _begin(self.store, first)
        await _begin(self.store, second)
        await self._suspend(first)

        with self.runtime.execution(_run(second), second), native_durable_call(
            _artifact(second.invocation_id)
        ), self.assertRaisesRegex(
            ExternalContinuationUnavailableError, "capacity is exhausted"
        ):
            await self.port.suspend(
                "provider-run-two",
                capability="hatchet.run",
                schema_id="report/v1",
                validate=_validate,
            )

        stored = await self.store.get_run(
            scope=RunScope("consumer", "user-1", "session-2", "run-two")
        )
        self.assertIsNotNone(stored)
        self.assertEqual(stored.status, "running")

    async def test_non_durable_tool_fails_before_persistence(self):
        """Keep unfinished external work out of ordinary managed tools."""

        request = _request("run-plain")
        await _begin(self.store, request)
        with self.runtime.execution(_run(request), request), self.assertRaisesRegex(
            ExternalContinuationUnavailableError, "durable=True"
        ):
            await self.port.suspend(
                "provider-run-plain",
                capability="hatchet.run",
                schema_id="report/v1",
                validate=_validate,
            )
        stored = await self.store.get_run(
            scope=RunScope("consumer", "user-1", "session-1", "run-plain")
        )
        self.assertEqual(stored.status, "running")

    async def test_principal_fails_before_cross_replica_wait_is_persisted(self):
        """Never resume a restricted invocation without its opaque authority."""

        request = _request(
            "run-principal",
            agent_principal=AgentRuntimePrincipal.create(
                permissions={"hatchet.run"}
            ),
        )
        await _begin(self.store, request)
        with self.runtime.execution(_run(request), request), native_durable_call(
            _artifact(request.invocation_id)
        ), self.assertRaisesRegex(
            ExternalContinuationUnavailableError, "Agent Runtime Principal"
        ):
            await self.port.suspend(
                "provider-run-principal",
                capability="hatchet.run",
                schema_id="report/v1",
                validate=_validate,
            )

        self.assertIsNone(
            await self.store.get_continuation_by_external_id(
                application_id="consumer",
                provider="hatchet",
                external_id="provider-run-principal",
            )
        )

    async def test_langgraph_replay_restores_existing_wait_on_second_replica(self):
        """Consume the claimed value when ToolNode re-enters plugin wait code."""

        from langchain_core.messages import AIMessage
        from langchain_core.tools import tool as langchain_tool
        from langgraph.graph import END, START, MessagesState, StateGraph
        from langgraph.prebuilt import ToolNode

        from harnest.application import CompiledApplication
        from harnest.checkpoint_langgraph import HarnestCheckpointSaver
        from harnest.durable import langgraph_durable_callable
        from harnest.runtime_langgraph import LangGraphRuntimeDriver
        from harnest.tool import tool

        store = self.store
        request = _request("run-graph")

        def replica(runtime: ExternalContinuationRuntime):
            """Build an independently compiled graph over the shared store."""

            port = runtime.invocation_port("hatchet")

            @tool(durable=True)
            async def wait_for_report() -> object:
                """Enter the plugin-style persisted wait from a durable tool."""

                handle = await port.suspend(
                    "provider-run-graph",
                    capability="hatchet.run",
                    schema_id="report/v1",
                    validate=_validate,
                )
                return await handle.result()

            native = langchain_tool(
                langgraph_durable_callable(wait_for_report)
            )

            async def request_report(_state):
                return {
                    "messages": [
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "wait_for_report",
                                    "args": {},
                                    "id": "graph-call-1",
                                    "type": "tool_call",
                                }
                            ],
                        )
                    ]
                }

            async def reply(state):
                return {
                    "messages": [
                        AIMessage(content=state["messages"][-1].content)
                    ]
                }

            builder = StateGraph(MessagesState)
            builder.add_node("request", request_report)
            builder.add_node("tools", ToolNode([native]))
            builder.add_node("reply", reply)
            builder.add_edge(START, "request")
            builder.add_edge("request", "tools")
            builder.add_edge("tools", "reply")
            builder.add_edge("reply", END)
            graph = builder.compile(checkpointer=HarnestCheckpointSaver(store))
            application = CompiledApplication(
                name="consumer",
                framework="langgraph",
                mode="advanced",
                target=graph,
                kind="advanced",
                checkpointer=store,
                session_store=store,
            )
            driver = LangGraphRuntimeDriver(application, session_store=store)
            runtime.bind_driver(driver)
            return driver

        first_runtime = ExternalContinuationRuntime(
            store, application_id="consumer"
        )
        first_driver = replica(first_runtime)
        await first_driver.create_session(
            session_id=request.session_id,
            user_id=request.user_id,
            state={},
        )
        first_run = _run(request)
        with first_runtime.execution(first_run, request), self.assertRaises(
            NativeDurableSuspended
        ):
            await first_driver.invoke(request)
        await first_runtime.arm(
            response_id=request.invocation_id,
            user_id=request.user_id,
            session_id=request.session_id,
        )

        second_runtime = ExternalContinuationRuntime(
            store, application_id="consumer"
        )
        second_driver = replica(second_runtime)
        second_runtime.application_port("hatchet").register_schema(
            "report/v1", _validate
        )
        try:
            claimed = await second_runtime.application_port("hatchet").complete(
                "provider-run-graph", {"report": "ready"}
            )
            messages = await second_driver.get_session_messages(
                session_id=request.session_id,
                user_id=request.user_id,
            )
        finally:
            await second_runtime.close()
            await second_driver.close()
            await first_runtime.close()
            await first_driver.close()

        self.assertEqual(claimed.status, "claimed")
        self.assertIsNotNone(messages)
        self.assertIn("ready", repr(messages))


if __name__ == "__main__":
    unittest.main()
