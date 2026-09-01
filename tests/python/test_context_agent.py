from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from harnest.application import CompiledApplication
from harnest.approval import request_human_approval
from harnest.client_tool import client_tool
from harnest.context import context
from harnest.context_agent import (
    AgentContinuationUnsupportedError,
    AgentInvocationUnavailableError,
    AgentPendingResponse,
    AgentResponse,
    LocalAgentRuntime,
    activate_context_agent,
)
from harnest.external_continuation import PendingExternalContinuation
from harnest.runtime_contract import (
    AgentInfo,
    InvocationRequest,
    InvocationResult,
    SessionConflictError,
    SessionRecord,
)
from harnest.runtime_task import TaskRuntimeManager
from harnest.task import CompiledTask, registration_for, task


@client_tool
async def _browser_action(value: str) -> str:
    """Ask the connected client to perform one fixture action."""

    return value


class _FakeDriver:
    """Exercise the portable boundary without choosing ADK or LangGraph."""

    def __init__(self) -> None:
        self.info = AgentInfo(
            id="root-agent",
            name="Root agent",
            description="fixture",
            card={},
            framework="langgraph",
            mode="managed",
        )
        self.sessions: dict[tuple[str, str], SessionRecord] = {}
        self.requests: list[InvocationRequest] = []
        self.cancelled_streams = 0
        self.cancelled_approvals = 0
        self.cancelled_client_tools = 0

    async def create_session(self, *, session_id, user_id, state):
        key = (user_id, session_id)
        if key in self.sessions:
            raise SessionConflictError(session_id)
        record = SessionRecord(session_id, user_id, dict(state))
        self.sessions[key] = record
        return record

    async def get_session(self, *, session_id, user_id):
        return self.sessions.get((user_id, session_id))

    async def list_sessions(self, *, user_id, after=None, limit=None):
        del after, limit
        return tuple(item for (owner, _), item in self.sessions.items() if owner == user_id)

    async def get_session_messages(self, *, session_id, user_id):
        return () if (user_id, session_id) in self.sessions else None

    async def update_session(self, *, session_id, user_id, state_delta):
        current = self.sessions.get((user_id, session_id))
        if current is None:
            return None
        updated = SessionRecord(
            current.id, current.user_id, {**dict(current.state), **dict(state_delta)}
        )
        self.sessions[(user_id, session_id)] = updated
        return updated

    async def delete_session(self, *, session_id, user_id):
        return self.sessions.pop((user_id, session_id), None) is not None

    async def invoke(self, request):
        self.requests.append(request)
        if request.input == "approval":
            try:
                async with request_human_approval(
                    action="fixture.approval",
                    message="Approve fixture?",
                ):
                    pass
            except asyncio.CancelledError:
                self.cancelled_approvals += 1
                raise
        if request.input == "client-tool":
            try:
                await _browser_action("fixture")
            except asyncio.CancelledError:
                self.cancelled_client_tools += 1
                raise
        event = {"type": "message", "role": "assistant", "text": request.input}
        return InvocationResult(
            text=str(request.input),
            events=(event,),
            result={"echo": request.input},
            session_id=request.session_id,
            metadata=request.metadata,
        )

    async def stream(self, request):
        self.requests.append(request)
        try:
            yield {"type": "message", "role": "assistant", "text": "first"}
            if request.input == "wait":
                await asyncio.Event().wait()
            yield {"type": "message", "role": "assistant", "text": "second"}
        finally:
            if request.input == "wait":
                self.cancelled_streams += 1

    async def close(self):
        return None


def _compiled_task(function):
    """Build only the task metadata required by TaskRuntimeManager."""

    authored = task(function)
    definition = registration_for(authored)
    assert definition is not None
    compiled = CompiledTask(
        name="harnest.fixture.tasks.invoke_child",
        source="tasks/invoke_child.py",
        definition=definition,
        authored=authored,
    )
    application = CompiledApplication(
        name="fixture",
        framework="langgraph",
        mode="managed",
        target=object(),
        tasks=(compiled,),
    )
    return compiled, application


class LocalAgentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_session_invokes_final_driver_and_opens_existing_session(self):
        driver = _FakeDriver()
        runtime = LocalAgentRuntime(driver, user_id="user-1")
        session = await runtime.create_session(state={"scope": "daily"})

        response = await session.invoke("hello", metadata={"source": "test"})
        reopened = await runtime.open_session(session.id)

        self.assertIsInstance(response, AgentResponse)
        self.assertEqual(response.output_text, "hello")
        self.assertEqual(response.as_dict()["result"], {"echo": "hello"})
        self.assertEqual(reopened.id, session.id)
        self.assertEqual(driver.requests[0].transport, "local")
        self.assertEqual(driver.requests[0].user_id, "user-1")

    async def test_stream_has_portable_events_terminal_result_and_cancellation(self):
        driver = _FakeDriver()
        runtime = LocalAgentRuntime(driver, user_id="user-1")
        session = await runtime.create_session()

        items = [item async for item in session.stream("hello")]
        self.assertEqual([item.kind for item in items], ["event", "event", "completed"])
        self.assertEqual(items[-1].as_dict()["type"], "agent.completed")
        self.assertEqual(items[-1].response.output_text, "firstsecond")

        waiting = session.stream("wait")
        first = await anext(waiting)
        self.assertEqual(first.kind, "event")
        await waiting.aclose()
        self.assertEqual(driver.cancelled_streams, 1)

    async def test_process_local_approval_fails_closed_and_cancels_waiter(self):
        driver = _FakeDriver()
        runtime = LocalAgentRuntime(driver, user_id="user-1")
        session = await runtime.create_session()

        with self.assertRaises(AgentContinuationUnsupportedError) as caught:
            await session.invoke("approval")

        self.assertEqual(caught.exception.kind, "approval")
        self.assertIn("process-local", str(caught.exception))
        self.assertEqual(driver.cancelled_approvals, 1)

    async def test_process_local_client_tool_fails_closed_and_cancels_waiter(self):
        driver = _FakeDriver()
        runtime = LocalAgentRuntime(driver, user_id="user-1")
        session = await runtime.create_session()

        with self.assertRaises(AgentContinuationUnsupportedError) as caught:
            await session.invoke("client-tool")

        self.assertEqual(caught.exception.kind, "client_tool")
        self.assertEqual(driver.cancelled_client_tools, 1)

    async def test_durable_external_wait_returns_typed_pending_handle(self):
        driver = _FakeDriver()
        runtime = LocalAgentRuntime(driver, user_id="user-1")
        session = await runtime.create_session()
        run = SimpleNamespace(
            activation=asyncio.Event(),
            notifications=asyncio.Queue(),
            task=asyncio.create_task(asyncio.sleep(0)),
        )
        run.notifications.put_nowait(
            ("external_continuation", PendingExternalContinuation("opaque", "jobs"))
        )

        with patch("harnest.context_agent.start_approval_run", return_value=run):
            response = await session.invoke("hello")

        self.assertIsInstance(response, AgentPendingResponse)
        self.assertEqual(response.status, "in_progress")
        self.assertEqual(
            response.as_dict()["pendingAction"],
            {"type": "external_continuation", "id": "opaque", "capability": "jobs"},
        )

    async def test_context_binding_is_retry_stable_and_revoked_after_task(self):
        driver = _FakeDriver()
        first_runtime = LocalAgentRuntime(
            driver,
            user_id="user-1",
            trigger="agent",
            identity_namespace="payload-1",
        )
        with activate_context_agent(first_runtime):
            first = await context.agent.create_session(key="digest")
            first_response = await first.invoke("hello")
        with self.assertRaises(AgentInvocationUnavailableError):
            await first.invoke("escaped")

        replay_runtime = LocalAgentRuntime(
            driver,
            user_id="user-1",
            trigger="agent",
            identity_namespace="payload-1",
        )
        with activate_context_agent(replay_runtime):
            replay = await context.agent.create_session(key="digest")
            replay_response = await replay.invoke("hello")

        self.assertEqual(first.id, replay.id)
        self.assertEqual(first_response.invocation_id, replay_response.invocation_id)

    async def test_context_agent_is_unavailable_without_task_binding(self):
        with self.assertRaises(AgentInvocationUnavailableError):
            await context.agent.create_session()

    async def test_task_manager_binds_automation_identity_to_child_session(self):
        observed = []

        async def invoke_child():
            """Invoke one root-agent turn from durable task execution."""

            session = await context.agent.create_session(state={"kind": "cron"})
            response = await session.invoke("scheduled")
            observed.append((session.user_id, response.output_text))
            return response.output_text

        compiled, application = _compiled_task(invoke_child)
        driver = _FakeDriver()
        manager = TaskRuntimeManager(application, backend=object())
        manager.bind_agent_driver(driver)

        result = await manager._call_authored(
            compiled,
            {},
            None,
            payload_id="cron-occurrence",
            trigger="cron",
        )

        self.assertEqual(result, "scheduled")
        self.assertEqual(observed, [("_harnest_automation", "scheduled")])
        self.assertEqual(driver.requests[0].transport, "task")

    async def test_task_child_inherits_user_and_public_metadata(self):
        observed = []

        async def invoke_child():
            """Capture the identity applied to a child root-agent turn."""

            session = await context.agent.create_session()
            response = await session.invoke("child", metadata={"child": True})
            observed.append(response.output_text)

        compiled, application = _compiled_task(invoke_child)
        driver = _FakeDriver()
        manager = TaskRuntimeManager(application, backend=object())
        manager.bind_agent_driver(driver)
        snapshot = {
            "framework": "langgraph",
            "agent_name": "parent",
            "invocation_id": "parent-invocation",
            "user_id": "parent-user",
            "session_id": "parent-session",
            "metadata": {"tenant": "acme"},
        }

        await manager._call_authored(
            compiled,
            {},
            snapshot,
            payload_id="task-payload",
            trigger="agent",
        )

        self.assertEqual(observed, ["child"])
        self.assertEqual(driver.requests[0].user_id, "parent-user")
        self.assertEqual(
            driver.requests[0].metadata,
            {"tenant": "acme", "child": True},
        )

    async def test_audit_never_contains_input_or_output_payload(self):
        driver = _FakeDriver()
        runtime = LocalAgentRuntime(driver, user_id="user-1")
        with self.assertLogs("harnest.agent.context_agent.audit", level="INFO") as logs:
            session = await runtime.create_session(state={"secret": "state-secret"})
            await session.invoke("prompt-secret")

        rendered = " ".join(logs.output)
        self.assertNotIn("state-secret", rendered)
        self.assertNotIn("prompt-secret", rendered)


if __name__ == "__main__":
    unittest.main()
