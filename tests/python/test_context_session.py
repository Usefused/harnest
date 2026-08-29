import unittest
from types import ModuleType
from unittest.mock import patch

from google.adk.events import Event, EventActions

from harnest.application import CompiledApplication
from harnest.context import (
    ContextUnavailableError,
    activate_context,
    create_agent_context,
    revoke_context,
)
from harnest.context_session import (
    SessionDataError,
    activate_session_context,
    session,
)
from harnest.runtime_contract import SessionRecord
from harnest.runtime_contract import InvocationRequest
from harnest.runtime_langgraph import LangGraphRuntimeDriver
from harnest.session import InMemorySessionStore
from harnest.session_adk import create_adk_session_service


def _agent_context(*, user_id="user-1", session_id="session-1"):
    return create_agent_context(
        framework="langgraph",
        agent_name="support",
        invocation_id="invocation-1",
        user_id=user_id,
        session_id=session_id,
        metadata={},
        resources={},
    )


class _FailingLease:
    record = SessionRecord("session-1", "user-1", {})

    async def replace_application_data(self, _data):
        raise RuntimeError("database unavailable")


class _HumanMessage:
    type = "human"

    def __init__(self, content):
        self.content = content

    def model_dump(self, **_kwargs):
        return {"type": self.type, "content": self.content}


class _SessionWritingGraph:
    def __init__(self):
        self.inputs = []

    async def ainvoke(self, graph_input, *, config):
        del config
        self.inputs.append(graph_input)
        await session.set("private-result", {"status": "ready"})
        return {"messages": [], "counter": 2}


class SessionContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = InMemorySessionStore()
        await self.store.start()
        await self.store.create(
            session_id="session-1",
            user_id="user-1",
            state={"framework": "original"},
        )

    async def asyncTearDown(self):
        await self.store.close()

    async def test_mutations_reuse_lease_and_survive_framework_replacement(self):
        active = _agent_context()
        with activate_context(active):
            async with self.store.acquire(
                session_id="session-1", user_id="user-1"
            ) as lease:
                with activate_session_context(
                    lease,
                    framework="langgraph",
                    invocation_id="invocation-1",
                ):
                    await session.set("draft", {"step": 1})
                    checkout = session.namespace("checkout")
                    await checkout.update({"currency": "GBP", "total": 20})
                    await checkout.delete("total")
                    detached = await session.get("draft")
                    detached["step"] = 99
                    await lease.replace_state({"framework": "replaced"})

                    self.assertEqual(await session.get("draft"), {"step": 1})
                    self.assertEqual(await checkout.get("currency"), "GBP")
                    saved = session.current()
        revoke_context(active)

        record = await self.store.get(
            session_id="session-1", user_id="user-1"
        )
        self.assertEqual(record.state, {"framework": "replaced"})
        self.assertEqual(
            record.application_data,
            {"draft": {"step": 1}, "checkout": {"currency": "GBP"}},
        )
        with self.assertRaises(ContextUnavailableError):
            await saved.get("draft")

    async def test_namespace_rejects_an_existing_scalar(self):
        active = _agent_context()
        with activate_context(active):
            async with self.store.acquire(
                session_id="session-1", user_id="user-1"
            ) as lease:
                with activate_session_context(
                    lease,
                    framework="langgraph",
                    invocation_id="invocation-1",
                ):
                    await session.set("checkout", "reserved")
                    with self.assertRaises(SessionDataError):
                        await session.namespace("checkout").set("step", 1)
        revoke_context(active)

    async def test_foreign_agent_context_cannot_use_bound_session(self):
        active = _agent_context(user_id="other-user")
        with activate_context(active):
            async with self.store.acquire(
                session_id="session-1", user_id="user-1"
            ) as lease:
                with activate_session_context(
                    lease,
                    framework="langgraph",
                    invocation_id="invocation-1",
                ):
                    with self.assertRaises(ContextUnavailableError):
                        await session.get("draft")
        revoke_context(active)

    async def test_audit_is_payload_free_for_commit_and_correlated_failure(self):
        active = _agent_context()
        with activate_context(active), self.assertLogs(
            "harnest.agent.session.audit", level="INFO"
        ) as logs:
            async with self.store.acquire(
                session_id="session-1", user_id="user-1"
            ) as lease:
                with activate_session_context(
                    lease,
                    framework="langgraph",
                    invocation_id="invocation-1",
                ):
                    await session.set("private", "never-log-this")
            with activate_session_context(
                _FailingLease(),
                framework="langgraph",
                invocation_id="invocation-1",
            ):
                with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                    await session.set("private", "still-never-log-this")
        revoke_context(active)

        self.assertEqual(
            [record.outcome for record in logs.records],
            ["committed", "failed"],
        )
        rendered = " ".join(record.getMessage() for record in logs.records)
        self.assertNotIn("never-log-this", rendered)
        self.assertFalse(any(hasattr(record, "user_id") for record in logs.records))


class FrameworkSessionIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_adk_event_replacement_preserves_application_data(self):
        store = InMemorySessionStore()
        await store.start()
        service = create_adk_session_service(store)
        native = await service.create_session(
            app_name="support",
            user_id="user-1",
            session_id="session-1",
            state={"count": 1},
        )
        active = _agent_context()

        with activate_context(active):
            async with service.execution_lease(
                user_id="user-1",
                session_id="session-1",
                invocation_id="invocation-1",
            ):
                await session.set("private-note", "not-for-the-model")
                await service.append_event(
                    native,
                    Event(
                        invocation_id="invocation-1",
                        author="agent",
                        actions=EventActions(state_delta={"count": 2}),
                    ),
                )
        revoke_context(active)

        stored = await store.get(session_id="session-1", user_id="user-1")
        projected = await service.get_session(
            app_name="support", user_id="user-1", session_id="session-1"
        )
        self.assertEqual(stored.application_data["private-note"], "not-for-the-model")
        self.assertEqual(projected.state, {"count": 2})
        self.assertNotIn("private-note", projected.state)
        await store.close()

    async def test_langgraph_replacement_preserves_unprojected_application_data(self):
        langchain_core = ModuleType("langchain_core")
        langchain_core.__path__ = []
        messages = ModuleType("langchain_core.messages")
        messages.HumanMessage = _HumanMessage
        target = _SessionWritingGraph()
        application = CompiledApplication(
            name="support",
            framework="langgraph",
            mode="advanced",
            target=target,
            kind="advanced",
        )
        store = InMemorySessionStore()
        driver = LangGraphRuntimeDriver(application, session_store=store)
        await driver.create_session(
            session_id="session-1",
            user_id="user-1",
            state={"counter": 1},
        )
        active = _agent_context()
        request = InvocationRequest(
            input="hello",
            user_id="user-1",
            session_id="session-1",
            invocation_id="invocation-1",
            metadata={},
            state_delta={},
        )

        with patch.dict(
            "sys.modules",
            {"langchain_core": langchain_core, "langchain_core.messages": messages},
        ), activate_context(active):
            await driver.invoke(request)
        revoke_context(active)

        stored = await store.get(session_id="session-1", user_id="user-1")
        self.assertEqual(
            stored.application_data,
            {"private-result": {"status": "ready"}},
        )
        self.assertNotIn("private-result", target.inputs[0])
        self.assertEqual(stored.state["counter"], 2)
        await driver.close()


if __name__ == "__main__":
    unittest.main()
