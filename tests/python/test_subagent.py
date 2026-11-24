"""Runtime SubAgents through real compiled ADK and LangGraph agents."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import importlib.util
import json
from pathlib import Path
import tempfile
import textwrap
import unittest

from fastapi.testclient import TestClient
from pydantic import BaseModel

from harnest.agent import (
    Agent,
    SubAgent,
    SubAgentClosedError,
    SubAgentError,
    SubAgentUnavailableError,
)
from harnest._subagent_owner import SubAgentOwner
from harnest.bundle import compile_artifact
from harnest.runtime import create_fastapi_app

import _subagent_fixture as fixture
from _session_store_fixture import write_session_store


ADK_AVAILABLE = importlib.util.find_spec("google.adk") is not None
LANGGRAPH_AVAILABLE = importlib.util.find_spec("langgraph") is not None
FRAMEWORKS = [
    name
    for name, available in (("adk", ADK_AVAILABLE), ("langgraph", LANGGRAPH_AVAILABLE))
    if available
]


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(value).lstrip(), encoding="utf-8")


def _write_agent(root: Path, framework: str) -> None:
    """A root agent whose tools start SubAgents in different ways."""

    _write(
        root / "agent.py",
        f"""
        from harnest.agent import Agent
        from _subagent_fixture import model

        root_agent = Agent(name="lead", model=model({framework!r}, "lead"), instruction="Delegate.")
        """,
    )
    _write(
        root / "lib" / "team.py",
        f"""
        from harnest import context
        from harnest.agent import Agent, tool
        from _subagent_fixture import model, record


        @tool
        async def probe() -> str:
            '''Report the identity this tool runs with.'''

            active = context.current()
            record("probe", {{
                "agent": active.agent_name,
                "parent": active.parent_agent_name,
                "user": active.user_id,
                "nested": active.depth > 0,
            }})
            return "ok"


        def researcher():
            return Agent(
                name="researcher",
                model=model({framework!r}, "researcher"),
                instruction="Research.",
                tools=[probe],
            )
        """,
    )
    _write(
        root / "tools" / "delegate.py",
        """
        from harnest.agent import SubAgent, tool
        from harnest.lib.team import researcher
        from _subagent_fixture import record


        @tool
        async def delegate(topic: str) -> str:
            '''Delegate research to a short-lived SubAgent.'''

            async with SubAgent(researcher()) as sub:
                record("open_inside", sub.is_open)
                record("session", sub.session_id)
                first = await sub.run(f"find {topic}")
                second = await sub.run("probe the sources")
                record("replies", (first.output_text, second.output_text))
            record("handle", sub)
            return second.output_text
        """,
    )
    _write(
        root / "tools" / "leak.py",
        """
        from harnest.agent import SubAgent, tool
        from harnest.lib.team import researcher
        from _subagent_fixture import record


        @tool
        async def leak(topic: str) -> str:
            '''Start a SubAgent and never close it.'''

            sub = await SubAgent(researcher()).start()
            reply = await sub.run(f"find {topic}")
            record("leaked", sub)
            return reply.output_text
        """,
    )
    _write(
        root / "tools" / "depth.py",
        """
        from harnest import _subagent
        from harnest.agent import SubAgent, SubAgentDepthError, tool
        from harnest.lib.team import researcher


        @tool
        async def depth(topic: str) -> str:
            '''Show that SubAgent nesting is bounded.'''

            token = _subagent._NESTING.set(_subagent.MAX_SUBAGENT_DEPTH)
            try:
                await SubAgent(researcher()).start()
            except SubAgentDepthError:
                return "depth refused"
            finally:
                _subagent._NESTING.reset(token)
            return "depth allowed"
        """,
    )
    _write(
        root / "lib" / "guarded.py",
        f"""
        from harnest.agent import Agent, tool
        from harnest.agent.approval import request_human_approval
        from _subagent_fixture import model, record


        @tool
        async def probe() -> str:
            '''Probe the sources once a person approves.'''

            async with request_human_approval(
                action="research.probe", message="Allow probing?", arguments={{}},
            ):
                record("approved_work", True)
                return "ok"


        def guarded_researcher():
            return Agent(
                name="researcher",
                model=model({framework!r}, "researcher"),
                instruction="Research.",
                tools=[probe],
            )
        """,
    )
    _write(
        root / "tools" / "approve.py",
        """
        from harnest.agent import SubAgent, tool
        from harnest.agent.approval import ApprovalDenied, _CURRENT_EXECUTION
        from harnest.lib.guarded import guarded_researcher
        from _subagent_fixture import record


        @tool
        async def approve(topic: str) -> str:
            '''Wait for the parent's client to approve a SubAgent operation.'''

            record("approval_run", _CURRENT_EXECUTION.get().run)
            async with SubAgent(guarded_researcher()) as sub:
                record("approval_handle", sub)
                try:
                    return (await sub.run("probe the sources")).output_text
                except ApprovalDenied:
                    return "denied"
        """,
    )
    _write_interactive_tools(root, framework)
    _write(
        root / "lifecycle" / "audit.py",
        """
        from harnest import lifecycle
        from harnest.agent import SubAgent
        from harnest.lib.team import researcher
        from _subagent_fixture import entries, record


        @lifecycle.tool.before
        def audit(context, call):
            record("tool_hook", (context.agent_name, call.name))
            return context.next()


        @lifecycle.model.before
        def watch_models(context, request):
            record("model_hook", request.messages[-1].text)
            for sub in entries("leaked"):
                record("leak_open_at_model", sub.is_open)
            return request


        @lifecycle.agent.before
        async def invocation_owned(context, value):
            # A SubAgent started outside any tool belongs to the invocation.
            if value.input == "hook":
                sub = await SubAgent(researcher()).start()
                record("hook_reply", (await sub.run("from a hook")).output_text)
                record("hook_handle", sub)
            return context.next()
        """,
    )
    _write(root / "instructions.md", "Delegate.\n")
    _write(
        root / "agent-card.yaml",
        """
        name: SubAgent lead
        description: Starts runtime SubAgents from tools and hooks.
        version: 0.1.0
        """,
    )
    write_session_store(root)


def _write_interactive_tools(root: Path, framework: str) -> None:
    """Exercise a protected client-tool declaration through nested SubAgents."""

    _write(
        root / "lib" / "interactive.py",
        f"""
        from pydantic import BaseModel
        from harnest.agent import Agent, SubAgent, client_tool, tool
        from harnest.agent.approval import require_human_approval
        from harnest.runtime_auth import _active_authenticated_principal
        from _subagent_fixture import model, record


        class Selection(BaseModel):
            text: str


        @client_tool
        @require_human_approval(message="Read the selection?")
        async def probe() -> Selection:
            '''Read the current selection from the connected client.'''
            raise AssertionError("client declarations must not execute")


        @tool
        async def nested_probe(topic: str) -> str:
            '''Run an interactive child inside another SubAgent.'''
            async with SubAgent(Agent(
                name="researcher", model=model({framework!r}, "researcher"),
                instruction="Research.", tools=[probe],
            )) as sub:
                record("interactive_handle", sub)
                reply = await sub.run("probe")
                principal = _active_authenticated_principal()
                record("resumed_user", None if principal is None else principal.user_id)
                return reply.output_text


        @tool
        async def interact(topic: str) -> str:
            '''Delegate a client request through two runtime SubAgents.'''
            async with SubAgent(Agent(
                name="lead", model=model({framework!r}, "lead"),
                instruction="Delegate.", tools=[nested_probe],
            )) as sub:
                record("interactive_handle", sub)
                return (await sub.run("nested_probe")).output_text
        """,
    )
    _write(root / "tools" / "interact.py", "from harnest.lib.interactive import interact\n")


@unittest.skipUnless(FRAMEWORKS, "ADK or LangGraph is required")
class SubAgentRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture.JOURNAL.clear()

    def _client(self, framework: str, tmp: str, *, authenticator=None) -> TestClient:
        """Compile the fixture with optional request-scoped authentication."""

        root = Path(tmp) / "source"
        artifact = Path(tmp) / "artifact"
        _write_agent(root, framework)
        compile_artifact(root, artifact, framework=framework)
        return TestClient(
            create_fastapi_app(artifact, authenticator=authenticator),
            raise_server_exceptions=True,
        )

    def _respond(self, client: TestClient, text: str) -> dict:
        response = client.post("/responses", json={"input": text})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_tool_owned_subagent_keeps_history_and_closes_with_its_block(self) -> None:
        for framework in FRAMEWORKS:
            with self.subTest(framework=framework), tempfile.TemporaryDirectory() as tmp:
                fixture.JOURNAL.clear()
                with self._client(framework, tmp) as client:
                    body = self._respond(client, "delegate")
                    sessions = client.get("/sessions").json()
                self.assertEqual(
                    body["outputText"], "root saw: sub turn 2: probe=ok"
                )
                self.assertEqual(fixture.entries("open_inside"), [True])
                self.assertEqual(
                    fixture.entries("replies"),
                    [("sub turn 1: find ants", "sub turn 2: probe=ok")],
                )
                (handle,) = fixture.entries("handle")
                self.assertFalse(handle.is_open)
                self.assertIsNone(handle.session_id)
                # The SubAgent's tool ran as the SubAgent, for the same user.
                (probe,) = fixture.entries("probe")
                self.assertEqual(probe["agent"], "researcher")
                self.assertTrue(probe["nested"])
                self.assertEqual(probe["user"], body.get("userId", probe["user"]))
                # Root tool hooks govern the SubAgent's tools too.
                hooked = fixture.entries("tool_hook")
                self.assertIn("delegate", [name for _, name in hooked])
                self.assertIn(("researcher", "probe"), hooked)
                # The SubAgent's private session never reaches the app's store.
                (private,) = fixture.entries("session")
                listed = sessions if isinstance(sessions, list) else sessions.get("sessions", sessions.get("items", []))
                self.assertNotIn(private, [item.get("id") for item in listed])
                self.assertEqual(len(listed), 1)

    def test_unclosed_subagent_is_closed_when_its_tool_call_ends(self) -> None:
        for framework in FRAMEWORKS:
            with self.subTest(framework=framework), tempfile.TemporaryDirectory() as tmp:
                fixture.JOURNAL.clear()
                with self._client(framework, tmp) as client:
                    body = self._respond(client, "leak")
                self.assertEqual(body["outputText"], "root saw: sub turn 1: find ants")
                (handle,) = fixture.entries("leaked")
                self.assertFalse(handle.is_open)
                # Closed when the tool call returned, before the root's next model call.
                self.assertEqual(fixture.entries("leak_open_at_model"), [False])
                # Root model hooks also saw the SubAgent's own model call.
                self.assertIn("find ants", fixture.entries("model_hook"))
                with self.assertRaises(SubAgentClosedError):
                    asyncio.run(handle.run("again"))

    def test_subagent_started_in_an_invocation_hook_is_owned_by_the_invocation(self) -> None:
        for framework in FRAMEWORKS:
            with self.subTest(framework=framework), tempfile.TemporaryDirectory() as tmp:
                fixture.JOURNAL.clear()
                with self._client(framework, tmp) as client:
                    client.post("/responses", json={"input": "hook"})
                self.assertEqual(fixture.entries("hook_reply"), ["sub turn 1: from a hook"])
                (handle,) = fixture.entries("hook_handle")
                self.assertFalse(handle.is_open)

    def test_subagent_tools_wait_for_human_approval(self) -> None:
        """Only approved child work runs, under the parent's response identity."""

        for framework in FRAMEWORKS:
            with self.subTest(framework=framework), tempfile.TemporaryDirectory() as tmp:
                fixture.JOURNAL.clear()
                with self._client(framework, tmp) as client:
                    body = self._respond(client, "approve")
                    self.assertEqual(body["status"], "requires_action")
                    self.assertEqual(fixture.entries("approved_work"), [])
                    (handle,) = fixture.entries("approval_handle")
                    self.assertTrue(handle.is_open)
                    self.assertNotEqual(handle.session_id, body["sessionId"])
                    action = body["requiredAction"]
                    self.assertEqual(action["callId"], body["id"])
                    completed = client.post(
                        f"/approvals/{action['id']}", json={"decision": "approve"},
                    ).json()
                self.assertEqual(completed["id"], body["id"])
                self.assertEqual(completed["outputText"], "root saw: sub turn 1: probe=ok")
                self.assertEqual(fixture.entries("approved_work"), [True])
                self.assertFalse(handle.is_open)

    def test_subagent_approval_denial_prevents_protected_work(self) -> None:
        """Keep the parent's denial contract without executing protected child work."""

        for framework in FRAMEWORKS:
            with self.subTest(framework=framework), tempfile.TemporaryDirectory() as tmp:
                fixture.JOURNAL.clear()
                with self._client(framework, tmp) as client:
                    body = self._respond(client, "approve")
                    completed = client.post(
                        f"/approvals/{body['requiredAction']['id']}",
                        json={"decision": "deny"},
                    )
                    self.assertEqual(completed.status_code, 200, completed.text)
                    # Denial replies immediately; join cleanup before shutting
                    # down the test server and its framework event loop.
                    client.portal.call(
                        asyncio.wait_for, fixture.entries("approval_run")[0].task, 5,
                    )
                self.assertEqual(completed.json()["status"], "denied")
                self.assertEqual(fixture.entries("approved_work"), [])
                self.assertFalse(fixture.entries("approval_handle")[0].is_open)

    def test_streaming_parent_publishes_subagent_approval(self) -> None:
        """A streaming root exposes the child's wait under its own response ID."""

        for framework in FRAMEWORKS:
            with self.subTest(framework=framework), tempfile.TemporaryDirectory() as tmp:
                fixture.JOURNAL.clear()
                with self._client(framework, tmp) as client:
                    response = client.post(
                        "/responses", json={"input": "approve", "stream": True},
                    )
                    events = [
                        json.loads(line[6:]) for line in response.text.splitlines()
                        if line.startswith("data: ")
                    ]
                    (requested,) = [event for event in events if event["type"] == "approval.requested"]
                    self.assertEqual(requested["approval"]["callId"], requested["responseId"])
                    completed = client.post(
                        f"/approvals/{requested['approval']['id']}",
                        json={"decision": "approve"},
                    )
                    self.assertEqual(completed.status_code, 200, completed.text)
                    self.assertEqual(completed.json()["id"], requested["responseId"])
                    self.assertEqual(completed.json()["outputText"], "root saw: sub turn 1: probe=ok")

    def test_nested_subagent_approval_then_typed_client_tool(self) -> None:
        """Resume nested children using the parent's approval and client endpoints."""

        from harnest.runtime_auth import AuthPrincipal

        class Authenticator:
            async def authenticate(self, connection):
                """Give each request its own revocable identity binding."""

                return AuthPrincipal(connection.headers.get("x-user", "alice"))

        for framework in FRAMEWORKS:
            with self.subTest(framework=framework), tempfile.TemporaryDirectory() as tmp:
                fixture.JOURNAL.clear()
                with self._client(framework, tmp, authenticator=Authenticator()) as client:
                    body = self._respond(client, "interact")
                    self.assertEqual(body["status"], "requires_action")
                    denied = client.post(
                        f"/approvals/{body['requiredAction']['id']}",
                        json={"decision": "approve"}, headers={"x-user": "mallory"},
                    )
                    self.assertEqual(denied.status_code, 404, denied.text)
                    approved = client.post(
                        f"/approvals/{body['requiredAction']['id']}",
                        json={"decision": "approve"},
                    )
                    self.assertEqual(approved.status_code, 200, approved.text)
                    pending = approved.json()
                    action = pending["requiredAction"]
                    self.assertEqual(action["type"], "client_tool")
                    self.assertEqual(action["name"], "probe")
                    self.assertEqual(action["callId"], body["id"])
                    self.assertEqual(pending["sessionId"], body["sessionId"])
                    endpoint = f"/client-tools/{action['id']}"
                    denied = client.post(
                        endpoint, json={"output": {"text": "wrong user"}},
                        headers={"x-user": "mallory"},
                    )
                    self.assertEqual(denied.status_code, 404, denied.text)
                    invalid = client.post(endpoint, json={"output": {"invalid": True}})
                    self.assertEqual(invalid.status_code, 409, invalid.text)
                    completed = client.post(endpoint, json={"output": {"text": "selected"}})
                    self.assertEqual(completed.status_code, 200, completed.text)
                    self.assertEqual(completed.json()["id"], body["id"])
                    self.assertIn("selected", completed.json()["outputText"])
                    self.assertEqual(completed.json()["status"], "completed")
                    replayed = client.post(endpoint, json={"output": {"text": "again"}})
                    self.assertIn(replayed.status_code, (404, 409))
                self.assertEqual(len(fixture.entries("interactive_handle")), 2)
                self.assertTrue(all(not sub.is_open for sub in fixture.entries("interactive_handle")))
                self.assertEqual(fixture.entries("resumed_user"), ["alice"])

    def test_nesting_is_bounded(self) -> None:
        for framework in FRAMEWORKS:
            with self.subTest(framework=framework), tempfile.TemporaryDirectory() as tmp:
                with self._client(framework, tmp) as client:
                    body = self._respond(client, "depth")
                self.assertEqual(body["outputText"], "root saw: depth refused")


@unittest.skipUnless(FRAMEWORKS, "ADK or LangGraph is required")
class SubAgentTaskTests(unittest.IsolatedAsyncioTestCase):
    """A task has no invocation pipeline; the task run owns and governs SubAgents."""

    async def test_task_run_owns_subagents_with_root_hooks_and_permissions(self) -> None:
        import time

        from harnest import context
        from harnest.agent import AgentRuntimePrincipal, tool
        from harnest.agent_principal import (
            activate_agent_principal,
            active_agent_principal,
            capability_is_available,
            create_agent_principal_binding,
            revoke_agent_principal,
        )
        from harnest.application import CompiledApplication
        from harnest.context import activate_context, revoke_context
        from harnest.lifecycle import LifecycleListener
        from harnest.task import CompiledTask, MemoryTaskStore, registration_for, task
        from test_task_runtime import _PausedWorker

        for framework in FRAMEWORKS:
            with self.subTest(framework=framework):
                fixture.JOURNAL.clear()

                @tool(permission="admin")
                async def wipe() -> str:
                    """An administrative tool the task's principal was never granted."""

                    return "wiped"

                @tool(permission="research.read")
                async def probe() -> str:
                    """Report the identity and grants this tool runs with."""

                    principal = active_agent_principal()
                    fixture.record("probe", context.current().agent_name)
                    fixture.record("permissions", None if principal is None else sorted(principal.permissions))
                    fixture.record("admin_available", capability_is_available(wipe))
                    return "ok"

                async def investigate(topic: str) -> str:
                    """Start a SubAgent outside any tool and leave it open."""

                    sub = await SubAgent(Agent(
                        name="researcher",
                        model=fixture.model(framework, "researcher"),
                        instruction="Research.",
                        tools=[probe],
                    )).start()
                    reply = await sub.run(f"probe {topic}")
                    fixture.record("task_handle", sub)
                    return reply.output_text

                def audit(hook_context, call):
                    fixture.record("tool_hook", (hook_context.agent_name, call.name))
                    return hook_context.next()

                authored = task(queue="research")(investigate)
                compiled = CompiledTask(
                    name="harnest.demo.tasks.investigate",
                    source="tasks/investigate.py",
                    definition=registration_for(authored),
                    authored=authored,
                )
                store = MemoryTaskStore()
                application = CompiledApplication(
                    name="demo",
                    framework=framework,
                    mode="managed",
                    target=object(),
                    tasks=(compiled,),
                    task_store=store,
                    cron_store=store,
                    lifecycle_extensions=(
                        LifecycleListener("before_tool", audit, 0, "lifecycle/audit.py", 1, "audit"),
                    ),
                )
                manager = _PausedWorker(application)
                await manager.start()
                try:
                    active = context.create_agent_context(
                        framework=framework,
                        agent_name="demo",
                        invocation_id="invocation",
                        user_id="alice",
                        session_id="session",
                        metadata={},
                        resources={},
                    )
                    binding = create_agent_principal_binding(
                        AgentRuntimePrincipal.create(permissions={"research.read"})
                    )
                    try:
                        with activate_context(active), activate_agent_principal(binding):
                            await authored.defer("ants")
                    finally:
                        revoke_context(active)
                        revoke_agent_principal(binding)
                    (record,) = await store.claim_tasks(
                        application_id="demo", queues=("research",), now=time.time(),
                        lease_seconds=30, limit=1,
                    )
                    await manager._execute_record(record)
                finally:
                    await manager.close()
                self.assertEqual(fixture.entries("probe"), ["researcher"])
                # The SubAgent's tools run with the owner's permissions, no more.
                self.assertEqual(fixture.entries("permissions"), [["research.read"]])
                self.assertEqual(fixture.entries("admin_available"), [False])
                self.assertEqual(fixture.entries("tool_hook"), [("researcher", "probe")])
                (handle,) = fixture.entries("task_handle")
                self.assertFalse(handle.is_open)


class SubAgentContractTests(unittest.IsolatedAsyncioTestCase):
    def _agent(self, **overrides) -> Agent:
        values = {"name": "helper", "model": "openai/gpt-4o-mini", "instruction": "Help."}
        values.update(overrides)
        return Agent(**values)

    async def test_requires_a_managed_run(self) -> None:
        sub = SubAgent(self._agent())
        with self.assertRaises(SubAgentUnavailableError):
            await sub.start()
        with self.assertRaises(SubAgentUnavailableError):
            async with SubAgent(self._agent()):
                pass

    async def test_lifecycle_errors_are_explicit(self) -> None:
        sub = SubAgent(self._agent())
        with self.assertRaisesRegex(SubAgentError, "not started"):
            await sub.run("hello")
        await sub.close()
        await sub.close()
        with self.assertRaises(SubAgentClosedError):
            await sub.start()
        with self.assertRaises(SubAgentClosedError):
            await sub.run("hello")

    def test_rejects_definitions_a_subagent_cannot_run(self) -> None:
        class Answer(BaseModel):
            text: str

        with self.assertRaisesRegex(TypeError, "Agent definition"):
            SubAgent("helper")  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "needs an instruction"):
            SubAgent(self._agent(instruction=None))
        with self.assertRaisesRegex(ValueError, "output_schema"):
            SubAgent(self._agent(output_schema=Answer))
        with self.assertRaisesRegex(ValueError, "sandboxes"):
            SubAgent(self._agent(sandboxes=["research"]))
        for timeout in (0, -1, float("inf"), True):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                SubAgent(self._agent(), request_timeout=timeout)


class SubAgentWaitLifetimeTests(unittest.IsolatedAsyncioTestCase):
    """Exercise child wait cleanup without model or transport timing dependencies."""

    @asynccontextmanager
    async def _child(self, *, parent_waits: bool = True):
        """Bind a real SubAgent to a small driver and optional parent wait channel."""

        from contextlib import ExitStack
        from harnest import context
        from harnest.agent.approval import InMemoryApprovalStore
        from harnest.client_tool import InMemoryClientToolStore
        from harnest.runtime_continuation import _run_wait_scope
        from harnest.tool_lifecycle import ToolLifecyclePipeline
        from test_context_agent import _FakeDriver

        class Host:
            def check_mcp_ownership(self, definition):
                """The fake driver has no MCP resources to share."""

            async def open_driver(self, definition):
                """Use the existing portable approval/client-tool driver fixture."""

                return _FakeDriver()

            def tool_pipeline(self):
                """Provide an empty root hook pipeline for task-style callers."""

                return ToolLifecyclePipeline(())

        store, clients = InMemoryApprovalStore(), InMemoryClientToolStore()
        run = store.create_run(user_id="alice", session_id="root", call_id="root-call")
        run.task = asyncio.current_task()
        active = context.create_agent_context(
            framework="langgraph", agent_name="root", invocation_id="root-call",
            user_id="alice", session_id="root", metadata={}, resources={},
            subagent_host=Host(), subagent_owner=SubAgentOwner("invocation"),
        )
        with ExitStack() as stack:
            stack.enter_context(context.activate_context(active))
            # Task/cron callers deliberately lack a transport continuation scope.
            if parent_waits:
                stack.enter_context(_run_wait_scope(store, clients, run, inherit_waits=False))
            async with SubAgent(Agent(
                name="child", model="openai/gpt-4o-mini", instruction="Help.",
            )) as child:
                yield child, store, clients, run

    async def test_unattended_waits_still_fail_closed(self) -> None:
        """A task without a connected caller cannot leave process-local requests."""

        async with self._child(parent_waits=False) as (child, _, _, _):
            for message in ("approval", "client-tool"):
                with self.subTest(message=message), self.assertRaisesRegex(
                    SubAgentError, "calling run cannot service",
                ):
                    await child.run(message)

    async def test_timeout_clears_child_approval_and_parent_remains_usable(self) -> None:
        """A child's deadline cancels only its wait, including its approval timer."""

        from harnest.agent.approval import ApprovalEnforcementError
        from harnest.context import AgentInvocationTimeout

        async with self._child() as (child, store, clients, run):
            task = asyncio.create_task(child.run("approval", request_timeout=0.05))
            kind, pending = await asyncio.wait_for(run.notifications.get(), 1)
            self.assertEqual(kind, "approval")
            with self.assertRaises(AgentInvocationTimeout):
                await task
            self.assertEqual(pending.status, "execution_cancelled")
            self.assertIsNone(store.run_for(pending))
            with self.assertRaises(ApprovalEnforcementError):
                store.decide(pending.id, user_id="alice", decision="approve")
            await self._resume_client(child, clients, run)

    async def test_cancellation_clears_client_request_and_parent_remains_usable(self) -> None:
        """Cancelling a child removes its client request and preserves parent authority."""

        async with self._child() as (child, _, clients, run):
            task = asyncio.create_task(child.run("client-tool"))
            kind, pending = await asyncio.wait_for(run.notifications.get(), 1)
            self.assertEqual(kind, "client_tool")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            with self.assertRaises(KeyError):
                await clients.submit(pending.id, user_id="alice", output="late")
            await self._resume_client(child, clients, run)

    async def _resume_client(self, child, clients, run) -> None:
        """Prove the original parent channel still resumes a later child turn."""

        task = asyncio.create_task(child.run("client-tool"))
        kind, pending = await asyncio.wait_for(run.notifications.get(), 1)
        self.assertEqual(kind, "client_tool")
        self.assertEqual(pending.call_id, "root-call")
        await clients.submit(pending.id, user_id="alice", output="accepted")
        self.assertEqual((await task).output_text, "client-tool")


class SubAgentMCPOwnershipTests(unittest.TestCase):
    """Closing a SubAgent must never close an MCP lifecycle another agent uses."""

    def test_shared_or_used_mcp_lifecycles_are_refused(self) -> None:
        from types import SimpleNamespace

        from harnest._subagent import SubAgentHost
        from harnest.mcp import MCPClient, MCPClientLifecycle

        def client(name: str, lifecycle: MCPClientLifecycle | None = None) -> MCPClient:
            return MCPClient.sse(f"https://{name}.example/sse", prefix=name, lifecycle=lifecycle)

        root_mcp = client("crm", MCPClientLifecycle())
        root = Agent(name="lead", model="openai/gpt-4o-mini", instruction="Lead.", mcp=[root_mcp])
        host = SubAgentHost(SimpleNamespace(managed_definition=root))

        def sub(*mcp: MCPClient) -> Agent:
            return Agent(name="helper", model="openai/gpt-4o-mini", instruction="Help.", mcp=list(mcp))

        with self.assertRaisesRegex(SubAgentError, "'crm'.*its own MCPClient"):
            host.check_mcp_ownership(sub(root_mcp))
        # The same lifecycle instance behind a different client is still shared.
        with self.assertRaisesRegex(SubAgentError, "its own MCPClient"):
            host.check_mcp_ownership(sub(client("crm_copy", root_mcp.lifecycle)))
        # A lifecycle another SubAgent already started or closed is used up.
        used = client("search", MCPClientLifecycle())
        used._lifecycle_controller._state = "closed"
        with self.assertRaisesRegex(SubAgentError, "'search'"):
            host.check_mcp_ownership(sub(used))
        # Fresh lifecycles and clients without lifecycle hooks are fine.
        host.check_mcp_ownership(sub(client("fresh", MCPClientLifecycle()), client("plain")))
        host.check_mcp_ownership(sub(client("plain_again")))
        SubAgentHost(SimpleNamespace(managed_definition=None)).check_mcp_ownership(sub(root_mcp))


class SubAgentOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_closes_newest_first_and_survives_failures(self) -> None:
        closed: list[str] = []

        class Child:
            def __init__(self, name: str, fail: bool = False) -> None:
                self.name, self.fail = name, fail

            async def _close_from_owner(self) -> None:
                closed.append(self.name)
                if self.fail:
                    raise RuntimeError("provider detail")

        owner = SubAgentOwner("tool call")
        first, broken, last = Child("first"), Child("broken", fail=True), Child("last")
        for child in (first, broken, last):
            owner.adopt(child)
        owner.release(last)
        await owner.aclose()
        self.assertEqual(closed, ["broken", "first"])
        with self.assertRaises(SubAgentUnavailableError):
            owner.adopt(Child("late"))
        await owner.aclose()
        self.assertEqual(closed, ["broken", "first"])


if __name__ == "__main__":
    unittest.main()
