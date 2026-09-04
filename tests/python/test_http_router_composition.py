"""Keep FastAPI's flat and retained include representations equally safe."""

import tempfile
import unittest
from pathlib import Path

from fastapi import APIRouter, FastAPI, routing
from fastapi.testclient import TestClient
from starlette.routing import Mount, Route

from _session_store_fixture import write_session_store
from harnest.extension_loader import discover_extensions
from harnest.http_routes import (
    HTTPRouteError,
    _route_contracts,
    create_http_route_extension,
    mount_http_route_extensions,
    validate_http_route_extensions,
)


async def _answer():
    """Return deterministic HTTP evidence without involving a model service."""

    return {"ok": True}


def _extension(router, identity="composed"):
    """Exercise the same factory validation used during compilation."""

    return create_http_route_extension(lambda _agent: router, identity=identity)


def _nested(path="/items/{item_id}"):
    """Include router-local and per-include prefixes at several levels."""

    child = APIRouter(prefix="/child")
    child.add_api_route(path, _answer, methods=["GET", "POST"])
    middle = APIRouter(prefix="/middle")
    middle.include_router(child, prefix="/v2")
    parent = APIRouter(prefix="/business")
    parent.include_router(middle, prefix="/v1")
    return parent


class HTTPRouterCompositionTests(unittest.TestCase):
    def test_nested_prefixes_match_served_paths_and_preserve_routers(self):
        """Validation must neither lose prefixes nor mutate native composition."""

        router = _nested()
        entries = tuple(router.routes)
        extension = _extension(router)
        path = "/business/v1/middle/v2/child/items/{item_id}"
        self.assertEqual(_route_contracts(router, identity="test"), (
            ("GET", path), ("POST", path),
        ))
        self.assertEqual(tuple(router.routes), entries)
        app = FastAPI()
        mount_http_route_extensions(app.router, (extension,), None)
        with TestClient(app) as client:
            for method in ("get", "post"):
                response = getattr(client, method)(path.replace("{item_id}", "42"))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"ok": True})

    def test_ordinary_unprefixed_include_compiles(self):
        """The reported parent.include_router(child) topology remains supported."""

        child, parent = APIRouter(), APIRouter()
        child.add_api_route("/custom/status", _answer, methods=["GET"])
        parent.include_router(child)
        self.assertIs(_extension(parent).router, parent)

    def test_reserved_paths_introduced_by_include_prefix_are_rejected(self):
        """A harmless local leaf must not bypass final namespace ownership."""

        leaf, middle, root = APIRouter(), APIRouter(), APIRouter()
        leaf.add_api_route("/execute", _answer, methods=["POST"], include_in_schema=False)
        middle.include_router(leaf, prefix="/responses")
        root.include_router(middle, include_in_schema=False)
        with self.assertRaisesRegex(HTTPRouteError, "reserved Harnest path"):
            _extension(root)

    def test_local_reserved_name_under_business_prefix_is_allowed(self):
        """Only the effective path, not the unmounted child path, is reserved."""

        child, parent = APIRouter(), APIRouter(prefix="/business")
        child.add_api_route("/responses", _answer, methods=["GET"])
        parent.include_router(child)
        self.assertEqual(_route_contracts(_extension(parent).router, identity="test"), (
            ("GET", "/business/responses"),
        ))

    def test_same_child_at_different_prefixes_is_not_deduplicated(self):
        """Every include occurrence represents independent endpoints."""

        child, parent = APIRouter(), APIRouter()
        child.add_api_route("/items", _answer, methods=["GET"])
        parent.include_router(child, prefix="/one")
        parent.include_router(child, prefix="/two")
        self.assertEqual(len(_route_contracts(_extension(parent).router, identity="test")), 2)
        parent.include_router(child, prefix="/one", include_in_schema=False)
        with self.assertRaisesRegex(HTTPRouteError, "conflicts with"):
            _extension(parent)

    def test_nested_and_direct_dynamic_contracts_conflict(self):
        """Duplicate templates still conflict across independent extensions."""

        first = _extension(_nested(), "nested")
        direct = APIRouter()
        direct.add_api_route(
            "/business/v1/middle/v2/child/items/{name}", _answer, methods=["GET"]
        )
        second = _extension(direct, "direct")
        with self.assertRaisesRegex(HTTPRouteError, "conflicts with nested"):
            validate_http_route_extensions((first, second))

    def test_nested_websocket_and_plain_starlette_routes_are_rejected(self):
        """Accepting FastAPI include wrappers must not broaden allowed leaves."""

        for kind in ("websocket", "starlette"):
            with self.subTest(kind=kind):
                child, parent = APIRouter(), APIRouter()
                if kind == "websocket":
                    child.add_api_websocket_route("/socket", _answer)
                else:
                    child.routes.append(Route("/plain", _answer))
                parent.include_router(child, prefix="/business")
                with self.assertRaisesRegex(HTTPRouteError, "only FastAPI HTTP routes"):
                    _extension(parent)

    def test_nested_catch_all_is_rejected(self):
        """Include prefixes cannot make a catch-all safe for runtime ownership."""

        with self.assertRaisesRegex(HTTPRouteError, "catch-all"):
            _extension(_nested("/{rest:path}"))

    @unittest.skipUnless(hasattr(routing, "_IncludedRouter"), "retained includes required")
    def test_wrapped_mount_and_unknown_leaf_are_not_silently_skipped(self):
        """FastAPI effective-route iterators omit unknown leaves; validation must not."""

        for route in (Mount("/mount", app=FastAPI()), object()):
            with self.subTest(kind=type(route).__name__):
                child, parent = APIRouter(), APIRouter()
                child.routes.append(route)
                parent.include_router(child, prefix="/business")
                with self.assertRaisesRegex(HTTPRouteError, "only FastAPI HTTP routes"):
                    _extension(parent)

    @unittest.skipUnless(hasattr(routing, "_IncludedRouter"), "retained includes required")
    def test_tampered_router_cycle_fails_without_global_deduplication(self):
        """Defensively reject cycles without suppressing legitimate repeated includes."""

        child, parent = APIRouter(), APIRouter()
        child.add_api_route("/items", _answer, methods=["GET"])
        parent.include_router(child, prefix="/business")
        child.routes.append(parent.routes[0])
        with self.assertRaisesRegex(HTTPRouteError, "router cycle"):
            _extension(parent)

    def test_extension_discovery_accepts_composed_factory_in_both_frameworks(self):
        """Compilation discovers native router composition before either runtime starts."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "agent"
            write_session_store(root)
            (root / "extensions" / "http.py").write_text(
                "from fastapi import APIRouter\n"
                "from harnest import lifecycle\n"
                "@lifecycle.http_routes\n"
                "def routes(agent):\n"
                "    child, parent = APIRouter(), APIRouter()\n"
                "    @child.get('/status')\n"
                "    async def status(): return {'ok': True}\n"
                "    parent.include_router(child, prefix='/business')\n"
                "    return parent\n",
                encoding="utf-8",
            )
            for framework in ("adk", "langgraph"):
                with self.subTest(framework=framework):
                    found = discover_extensions(root / "extensions", framework=framework)
                    self.assertEqual(len(found.http_routes), 1)
