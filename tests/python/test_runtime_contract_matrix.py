import asyncio
from contextlib import contextmanager
import importlib.util
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from _session_store_fixture import write_session_store
from harnest.application import CompiledApplication
from harnest.bundle import compile_artifact
from harnest.context import context
from harnest.credentials import Credential, CredentialProvider
from harnest.lifecycle import LifecycleListener
from harnest.neutral_runtime import (
    AgentInfo,
    InvocationRequest,
    InvocationResult,
)
from harnest.runtime import _runtime_driver, create_fastapi_app
from harnest.runtime_extensions import ExtensionRuntimeDriver
from harnest.runtime_session import StorageRuntimeDriver
from harnest.session import InMemorySessionStore


ADK_AVAILABLE = importlib.util.find_spec("google.adk") is not None
LANGGRAPH_AVAILABLE = importlib.util.find_spec("langgraph") is not None


def _write(path: Path, value: str) -> None:
    """Write one dedented authored fixture and create required parent folders."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(value).lstrip(), encoding="utf-8")


def _write_contract_agent(root: Path) -> None:
    """Create one portable graph that exercises success, failure, and approval."""

    _write(
        root / "agent.py",
        """
        from harnest.approval import request_human_approval
        from harnest.graph import START, Edge, Event, Graph


        async def protected_send(value: str) -> str:
            '''Protect one deterministic operation behind dynamic approval.'''

            async with request_human_approval(
                action="contract.send",
                message="Approve sending the record?",
                arguments={"value": value},
            ):
                return f"approved:{value}"


        async def execute(value):
            '''Produce success, failure, or suspension from the fixture input.'''

            if value == "explode":
                raise RuntimeError("private backend detail")
            if value.startswith("approve:"):
                value = await protected_send(value.removeprefix("approve:"))
            return Event(output=value, message=value)


        root_agent = Graph(
            name="contract_graph",
            nodes={"execute": execute},
            edges=(Edge(START, "execute"),),
        )
        """,
    )
    _write(
        root / "agent-card.yaml",
        """
        name: Contract graph
        description: Exercises the portable runtime contract.
        version: 0.1.0
        """,
    )
    write_session_store(root)


def _write_contract_extensions(root: Path) -> None:
    """Add authentication and a custom route backed by the shared invoker."""

    _write(
        root / "extensions" / "gateway.py",
        """
        from fastapi import APIRouter, Request
        from harnest.lifecycle import lifecycle
        from harnest.runtime_auth import AuthPrincipal, AuthenticationError


        @lifecycle.authenticate
        def authenticate(connection, principal):
            '''Resolve fixture identity from one explicit request header.'''

            del principal
            user_id = connection.headers.get("x-user")
            if not user_id:
                raise AuthenticationError()
            return AuthPrincipal(user_id)


        @lifecycle.http_routes
        def http_routes(agent):
            '''Expose a business endpoint through the shared root invoker.'''

            router = APIRouter(prefix="/business")

            @router.post("/invoke")
            async def invoke(request: Request):
                '''Map one business request onto the compiled root agent.'''

                payload = await request.json()
                response = await agent.invoke(
                    connection=request,
                    input=payload["input"],
                    session_id=payload.get("sessionId"),
                    metadata={"source": "custom"},
                )
                return response.as_dict()

            return router
        """,
    )


def _common_response(value: dict) -> dict:
    """Select transport guarantees without comparing native graph result shapes."""

    # ADK emits the portable Graph's scalar result, while LangGraph retains its
    # native state mapping. That established result detail is not a wire drift.
    return {
        "status": value["status"],
        "outputText": value["outputText"],
        "hasOutput": bool(value["output"]),
        "metadata": value["metadata"],
    }


def _exercise_http_contract(client: TestClient, framework: str) -> dict:
    """Exercise the same non-streaming contract against one compiled backend."""

    alice = {"x-user": "alice"}
    bob = {"x-user": "bob"}
    session_id = f"owned-{framework}"
    unauthorized_response = client.post("/responses", json={"input": "hello"})
    unauthorized_custom = client.post(
        "/business/invoke", json={"input": "hello"}
    )
    created = client.post(
        "/sessions", headers=alice, json={"id": session_id, "state": {"count": 1}}
    )
    hidden_session = client.get(f"/sessions/{session_id}", headers=bob)
    hidden_response = client.post(
        "/responses",
        headers=bob,
        json={"input": "hello", "sessionId": session_id},
    )
    hidden_custom = client.post(
        "/business/invoke",
        headers=bob,
        json={"input": "hello", "sessionId": session_id},
    )
    invalid_response = client.post("/responses", headers=alice, json={})
    standard = client.post(
        "/responses",
        headers=alice,
        json={
            "input": "hello",
            "sessionId": session_id,
            "metadata": {"source": "responses"},
        },
    )
    custom = client.post(
        "/business/invoke",
        headers=alice,
        json={"input": "custom", "sessionId": session_id},
    )
    failed = client.post(
        "/responses",
        headers=alice,
        json={"input": "explode", "sessionId": session_id},
    )
    failed_custom = client.post(
        "/business/invoke",
        headers=alice,
        json={"input": "explode", "sessionId": session_id},
    )
    approval_session = f"approval-{framework}"
    client.post("/sessions", headers=alice, json={"id": approval_session})
    required = client.post(
        "/business/invoke",
        headers=alice,
        json={"input": "approve:record", "sessionId": approval_session},
    )
    required_value = required.json()
    resumed = client.post(
        f"/approvals/{required_value['requiredAction']['id']}",
        headers=alice,
        json={"decision": "approve"},
    )

    # Compare only stable public fields; response and approval identifiers are
    # deliberately generated independently by each runtime instance.
    return {
        "unauthorized": (
            unauthorized_response.status_code,
            unauthorized_custom.status_code,
        ),
        "session": {
            "create": created.status_code,
            "owner": created.json()["userId"],
            "state": created.json()["state"],
            "hidden": (
                hidden_session.status_code,
                hidden_response.status_code,
                hidden_custom.status_code,
            ),
        },
        "invalid": (
            invalid_response.status_code,
            invalid_response.json()["detail"],
        ),
        "standard": _common_response(standard.json()),
        "custom": _common_response(custom.json()),
        "failure": (
            failed.status_code,
            "private backend detail" in failed.text,
            failed_custom.status_code,
            "private backend detail" in failed_custom.text,
        ),
        "approval": {
            "requested": required.status_code,
            "status": required_value["status"],
            "action": required_value["requiredAction"]["action"],
            "completed": resumed.status_code,
            "result": _common_response(resumed.json()),
        },
    }


class ManagedRuntimeContractTests(unittest.TestCase):
    def test_adk_and_langgraph_share_the_non_streaming_http_contract(self):
        """Keep public runtime behavior identical across managed backends."""

        available = [
            framework
            for framework, installed in (
                ("adk", ADK_AVAILABLE),
                ("langgraph", LANGGRAPH_AVAILABLE),
            )
            if installed
        ]
        if len(available) < 2:
            self.skipTest("ADK and LangGraph are required for the parity matrix")

        outcomes = {}
        for framework in available:
            with (
                self.subTest(framework=framework),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp) / "source"
                artifact = Path(tmp) / "artifact"
                _write_contract_agent(root)
                _write_contract_extensions(root)
                compile_artifact(root, artifact, framework=framework)
                with TestClient(
                    create_fastapi_app(artifact), raise_server_exceptions=False
                ) as client:
                    outcomes[framework] = _exercise_http_contract(client, framework)

        self.assertEqual(outcomes["adk"], outcomes["langgraph"])
        self.assertEqual(outcomes["adk"]["unauthorized"], (401, 401))
        self.assertEqual(outcomes["adk"]["session"]["hidden"], (404, 404, 404))
        self.assertEqual(outcomes["adk"]["failure"], (500, False, 500, False))
        self.assertEqual(outcomes["adk"]["approval"]["status"], "requires_action")
        self.assertEqual(
            outcomes["adk"]["approval"]["result"]["outputText"],
            "approved:record",
        )


class RecordingStore(InMemorySessionStore):
    """Record storage ownership without changing the in-memory contract."""

    def __init__(self, events: list[str]) -> None:
        """Retain the shared journal used to assert lifecycle ordering."""

        super().__init__()
        self._events = events

    async def start(self) -> None:
        """Record when the runtime accepts ownership of storage."""

        self._events.append("storage:start")

    async def close(self) -> None:
        """Record storage cleanup after the wrapped backend has closed."""

        self._events.append("storage:close")
        await super().close()


class RecordingCredentialProvider(CredentialProvider):
    """Record private provider lifetime around the composed runtime."""

    def __init__(self, events: list[str]) -> None:
        """Retain the shared journal used to assert lifecycle ordering."""

        self._events = events

    async def start(self) -> None:
        """Record provider startup before lifecycle resources are entered."""

        self._events.append("credentials:start")

    async def resolve(self, _request) -> Credential | None:
        """Return a harmless value when a fixture resolves credentials."""

        return Credential("fixture")

    async def close(self) -> None:
        """Record provider cleanup after all other owned resources."""

        self._events.append("credentials:close")


class RecordingDriver:
    """Minimal backend that exposes ordering through invocation and close."""

    def __init__(self, events: list[str]) -> None:
        """Retain the shared journal and describe a LangGraph backend."""

        self._events = events
        self.info = AgentInfo(
            id="root",
            name="root",
            description="fixture",
            card={},
            framework="langgraph",
            mode="managed",
        )

    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        """Record the innermost execution after outer resources have started."""

        self._events.append("backend:invoke")
        return InvocationResult(
            text="ok",
            events=({"type": "message", "text": "ok"},),
            result="ok",
            session_id=request.session_id,
            metadata=request.metadata,
        )

    async def close(self) -> None:
        """Record framework shutdown before outer resource cleanup."""

        self._events.append("backend:close")


class RuntimeCompositionContractTests(unittest.TestCase):
    def test_extensions_own_storage_and_backend_in_documented_order(self):
        """Pin wrapper shape and startup/cleanup ownership as one contract."""

        events: list[str] = []
        backend = RecordingDriver(events)
        store = RecordingStore(events)
        provider = RecordingCredentialProvider(events)

        @contextmanager
        def application_resource():
            """Record an application resource lifetime around invocation work."""

            events.append("resource:start")
            try:
                yield object()
            finally:
                events.append("resource:close")

        listener = LifecycleListener(
            "resource", application_resource, 0, "resources.py", 1, "resource"
        )

        def before(_lifecycle, value):
            """Prove custom storage is started before lifecycle access begins."""

            events.append(f"before:{context.storage('users') is store}")
            return value

        before_listener = LifecycleListener(
            "before_invoke", before, 0, "before.py", 1, "before"
        )
        application = CompiledApplication(
            name="root",
            framework="langgraph",
            mode="managed",
            target=object(),
            extensions=(listener, before_listener),
            session_store=store,
            custom_stores={"users": store},
            credential_provider=provider,
        )
        with patch(
            "harnest.runtime_langgraph.LangGraphRuntimeDriver", return_value=backend
        ):
            runtime = _runtime_driver(application)

        self.assertIsInstance(runtime, StorageRuntimeDriver)
        self.assertIsInstance(runtime._driver, ExtensionRuntimeDriver)
        self.assertIs(runtime._driver._driver, backend)
        request = InvocationRequest(
            input="hello",
            user_id="alice",
            session_id="session",
            invocation_id="invocation",
            metadata={},
            state_delta={},
        )
        async def exercise_runtime():
            """Create the session that real transports establish before invocation."""

            await store.create(
                session_id=request.session_id,
                user_id=request.user_id,
                state={},
            )
            result = await runtime.invoke(request)
            await runtime.close()
            return result

        result = asyncio.run(exercise_runtime())

        self.assertEqual(result.text, "ok")
        self.assertEqual(
            events,
            [
                "storage:start",
                "credentials:start",
                "resource:start",
                "before:True",
                "backend:invoke",
                "backend:close",
                "resource:close",
                "credentials:close",
                "storage:close",
            ],
        )


if __name__ == "__main__":
    unittest.main()
