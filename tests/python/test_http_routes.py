import asyncio
from dataclasses import replace
import tempfile
import unittest
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.testclient import TestClient

from _session_store_fixture import write_session_store
from harnest import AgentRuntimePrincipal
from harnest.extension_loader import ExtensionDiscoveryError, discover_extensions
from harnest.http_routes import (
    AgentInvoker,
    HTTPRouteError,
    create_http_route_extension,
    validate_http_route_extensions,
)
from harnest.neutral_runtime import create_neutral_app
from test_neutral_runtime import ApprovalDriver, FakeDriver, HeaderAuthenticator


def _application_router(invoker: AgentInvoker) -> APIRouter:
    """Expose a business endpoint that maps its payload onto one agent turn."""

    router = APIRouter(prefix="/threadify")

    @router.post("/execute")
    async def execute(request: Request):
        payload = await request.json()
        response = await invoker.invoke(
            connection=request,
            input=payload["message"],
            session_id=payload.get("sessionId"),
            metadata={"source": "threadify"},
        )
        return response.as_dict()

    return router


def _principal_router(invoker: AgentInvoker) -> APIRouter:
    """Model the application gateway mapping auth into runtime grants."""

    router = APIRouter(prefix="/restricted")

    @router.post("/execute")
    async def execute(request: Request):
        return (
            await invoker.invoke(
                connection=request,
                input="hello",
                agent_principal=AgentRuntimePrincipal.create(
                    permissions={"tickets.read"}
                ),
            )
        ).as_dict()

    return router


class HTTPRouteDiscoveryTests(unittest.TestCase):
    def test_discovers_one_argument_router_factory_without_invocation_listener(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "agent"
            write_session_store(root)
            (root / "extensions" / "http.py").write_text(
                "from fastapi import APIRouter\n"
                "from harnest import lifecycle\n"
                "@lifecycle.http_routes\n"
                "def routes(agent):\n"
                "    router = APIRouter()\n"
                "    @router.get('/custom/status')\n"
                "    async def status(): return {'ok': True}\n"
                "    return router\n",
                encoding="utf-8",
            )

            discovered = discover_extensions(
                root / "extensions", framework="langgraph"
            )

        self.assertEqual(len(discovered.http_routes), 1)
        self.assertNotIn("http_routes", [item.phase for item in discovered.listeners])

    def test_rejects_wrong_factory_signature_and_reserved_routes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "agent"
            write_session_store(root)
            routes = root / "extensions" / "http.py"
            routes.write_text(
                "from harnest import lifecycle\n"
                "@lifecycle.http_routes\n"
                "def routes(): return object()\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ExtensionDiscoveryError, "exactly one AgentInvoker"
            ):
                discover_extensions(root / "extensions", framework="adk")

            routes.write_text(
                "from fastapi import APIRouter\n"
                "from harnest import lifecycle\n"
                "@lifecycle.http_routes\n"
                "def routes(agent):\n"
                "    router = APIRouter()\n"
                "    @router.post('/responses')\n"
                "    async def responses(): return {}\n"
                "    return router\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ExtensionDiscoveryError, "reserved Harnest path"
            ):
                discover_extensions(root / "extensions", framework="adk")

    def test_rejects_duplicate_dynamic_route_contracts(self):
        def first(_agent):
            router = APIRouter()

            @router.get("/custom/items/{item_id}")
            async def item(item_id: str):
                return {"id": item_id}

            return router

        def second(_agent):
            router = APIRouter()

            @router.get("/custom/items/{name}")
            async def item(name: str):
                return {"name": name}

            return router

        extensions = (
            create_http_route_extension(first, identity="first.py:1:first"),
            create_http_route_extension(second, identity="second.py:1:second"),
        )

        with self.assertRaisesRegex(HTTPRouteError, "conflicts with"):
            validate_http_route_extensions(extensions)


class AgentInvokerTests(unittest.TestCase):
    def test_custom_route_passes_application_authorized_runtime_principal(self):
        driver = FakeDriver()
        driver.info = replace(
            driver.info, agent_principal_projection_complete=True
        )
        extension = create_http_route_extension(
            _principal_router, identity="http.py:1:http_routes"
        )
        app = create_neutral_app(
            driver,
            authenticator=HeaderAuthenticator(),
            http_routes=(extension,),
        )

        with TestClient(app) as client:
            response = client.post(
                "/restricted/execute", headers={"x-test-user": "alice"}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            driver.invocations[0].agent_principal.permissions,
            frozenset({"tickets.read"}),
        )

    def test_incomplete_projection_fails_before_implicit_session_creation(self):
        driver = FakeDriver()
        driver.info = replace(
            driver.info, agent_principal_projection_complete=False
        )
        extension = create_http_route_extension(
            _principal_router, identity="http.py:1:http_routes"
        )

        with TestClient(
            create_neutral_app(driver, http_routes=(extension,)),
            raise_server_exceptions=False,
        ) as client:
            response = client.post("/restricted/execute")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(driver.sessions, {})

    def test_invoker_rejects_principal_lookalikes_before_dispatch(self):
        invoker = AgentInvoker()
        dispatched = False

        async def invoke(*_args):
            nonlocal dispatched
            dispatched = True
            return {}

        invoker._bind(invoke)
        with self.assertRaisesRegex(TypeError, "agent_principal"):
            asyncio.run(
                invoker.invoke(
                    connection=object(), input="hello", agent_principal=object()
                )
            )

        self.assertFalse(dispatched)

    def test_custom_route_uses_authenticated_identity_and_shared_runtime(self):
        driver = FakeDriver()
        extension = create_http_route_extension(
            _application_router, identity="http.py:1:http_routes"
        )
        app = create_neutral_app(
            driver,
            authenticator=HeaderAuthenticator(),
            http_routes=(extension,),
        )

        with TestClient(app) as client:
            rejected = client.post(
                "/threadify/execute", json={"message": "hello"}
            )
            session = client.post(
                "/sessions",
                headers={"x-test-user": "alice"},
                json={"id": "threadify-session"},
            )
            accepted = client.post(
                "/threadify/execute",
                headers={"x-test-user": "alice"},
                json={
                    "message": "hello",
                    "sessionId": session.json()["id"],
                },
            )
            cross_user = client.post(
                "/threadify/execute",
                headers={"x-test-user": "bob"},
                json={"message": "hello", "sessionId": "threadify-session"},
            )

        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(cross_user.status_code, 404)
        self.assertEqual(accepted.json()["outputText"], "hello")
        self.assertEqual(accepted.json()["result"], 42)
        self.assertEqual(driver.invocations[0].user_id, "alice")
        self.assertEqual(driver.invocations[0].transport, "custom_http")
        self.assertEqual(driver.invocations[0].metadata, {"source": "threadify"})

    def test_custom_route_and_standard_approval_endpoint_share_continuation(self):
        driver = ApprovalDriver()
        extension = create_http_route_extension(
            _application_router, identity="http.py:1:http_routes"
        )

        with TestClient(
            create_neutral_app(driver, http_routes=(extension,))
        ) as client:
            required = client.post(
                "/threadify/execute", json={"message": "record"}
            ).json()
            resumed = client.post(
                f"/approvals/{required['requiredAction']['id']}",
                json={"decision": "approve"},
            )

        self.assertEqual(required["status"], "requires_action")
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.json()["outputText"], "sent:record")
        self.assertEqual(driver.before_protected, 1)
        self.assertEqual(driver.after_protected, 1)

    def test_unbound_invoker_fails_outside_a_serving_application(self):
        with self.assertRaisesRegex(RuntimeError, "only while serving"):
            asyncio.run(
                AgentInvoker().invoke(connection=object(), input="hello")
            )

    def test_authenticated_request_cannot_invoke_from_a_detached_task(self):
        captured = {}

        def routes(invoker):
            router = APIRouter()

            @router.post("/custom/capture")
            async def capture(request: Request):
                captured.update(invoker=invoker, request=request)
                return {"captured": True}

            return router

        extension = create_http_route_extension(
            routes, identity="http.py:1:http_routes"
        )
        with TestClient(
            create_neutral_app(
                FakeDriver(),
                authenticator=HeaderAuthenticator(),
                http_routes=(extension,),
            )
        ) as client:
            response = client.post(
                "/custom/capture", headers={"x-test-user": "alice"}
            )

        self.assertEqual(response.status_code, 200)
        with self.assertRaisesRegex(HTTPException, "Authentication context"):
            asyncio.run(
                captured["invoker"].invoke(
                    connection=captured["request"], input="late"
                )
            )


if __name__ == "__main__":
    unittest.main()
