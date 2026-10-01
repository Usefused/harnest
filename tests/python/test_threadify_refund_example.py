"""Check the example's real HTTP boundary and governed action ordering."""

from contextlib import asynccontextmanager
import importlib.util
import inspect
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx


_ROOT = Path(__file__).resolve().parents[2] / "examples/threadify-refund"


def _load(name: str, path: Path) -> ModuleType:
    """Load an example module without giving its folder a Python package name."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Step:
    """Record the SDK calls relevant to the effect boundary."""

    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self.calls = calls

    def idempotency_key(self, key: str):
        """Keep the builder shape while exposing the chosen stable key."""
        assert key
        return self

    def add_context(self, values: dict):
        """Accept authored business evidence for the fake Threadify step."""
        assert isinstance(values, dict)
        return self

    async def success(self, *, wait_for: bool):
        """Represent validation of a reported step before continuing."""
        assert wait_for
        self.calls.append(self.name)


class _Workflow:
    """Fake a shared Threadify thread with an explicit permission boundary."""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.denied = False
        self.unavailable = False

    def step(self, name: str) -> _Step:
        """Return an SDK-shaped report builder for a named step."""
        return _Step(name, self.calls)

    async def wait_for(self, name: str, options):
        """Grant one invocation or fail before the payment call."""
        self.calls.append(f"wait:{name}")
        if self.unavailable:
            raise _ThreadifyError("Live thread state is unavailable", "THREADIFY_PERMISSION_DENIED", "unavailable")
        if self.denied:
            raise RuntimeError("contract denied")
        return SimpleNamespace(invocation_id=options.invocation_id)


class _ThreadifyError(Exception):
    """Expose the Engine error fields consumed by the example's failure mapping."""

    def __init__(self, message: str, code: str, decision: str) -> None:
        super().__init__(message)
        self.code = code
        self.decision = SimpleNamespace(decision=decision)


class ThreadifyRefundExampleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        """Load the authored tool using only the SDK surface it consumes."""
        sdk = ModuleType("threadify")
        sdk.ThreadifyError = _ThreadifyError
        sdk.WaitOptions = lambda **values: SimpleNamespace(**values)
        with patch.dict(sys.modules, {"threadify": sdk}):
            self.tool_module = _load("refund_tool_example", _ROOT / "tools/issue_refund.py")
        self.tool = inspect.unwrap(self.tool_module.issue_refund)
        self.calls: list[str] = []
        self.workflow = _Workflow(self.calls)
        self.integration = SimpleNamespace(join_workflow=AsyncMock(side_effect=self._open_workflow))
        self.order = {
            "id": "O-42", "amount": 42, "currency": "GBP", "thread_id": "thread-42",
        }

    async def _open_workflow(self, thread_id: str, **options):
        """Verify the backend's authorized thread ID and processor role."""
        self.assertEqual((thread_id, options), (
            "thread-42", {"role": "processor"},
        ))
        self.calls.append("open")
        return self.workflow

    async def _issue(self, client, order, actor, invocation_id):
        """Observe whether the refund happens after both independent grants."""
        self.calls.append("payment")
        self.assertEqual((order, actor), (self.order, "alice"))
        self.assertTrue(invocation_id)
        return {"receipt": "RF-00000001"}

    @asynccontextmanager
    async def _approval(self, **options):
        """Represent Harnest's authenticated human approval continuation."""
        self.assertEqual(options["action"], "refund.issue")
        self.calls.append("human_approval")
        yield

    async def _invoke(self):
        """Run the authored tool with a fake Harnest scope and live SDK order."""
        client = AsyncMock()
        client.__aenter__.return_value = object()
        with (
            patch.dict(os.environ, {"REFUND_API_URL": "http://127.0.0.1:8091"}),
            patch.object(self.tool_module.httpx, "AsyncClient", return_value=client),
            patch.object(self.tool_module.context, "current", return_value=SimpleNamespace(
                user_id="alice", invocation_id="turn-1",
            )),
            patch.object(self.tool_module.context, "resource", return_value=self.integration),
            patch.object(self.tool_module, "request_human_approval", self._approval),
            patch.object(self.tool_module, "_authorized_order", AsyncMock(return_value=self.order)),
            patch.object(self.tool_module, "_issue", AsyncMock(side_effect=self._issue)),
        ):
            return await self.tool("O-42")

    async def test_refund_requires_human_and_contract_permission(self):
        """The side effect follows approval, validated evidence, and a wait grant."""
        result = await self._invoke()
        self.assertEqual(result, {"status": "issued", "receipt": "RF-00000001"})
        self.assertEqual(self.calls, [
            "human_approval", "open", "approval", "wait:refund_issued", "payment",
            "refund_issued", "refund_complete",
        ])

    async def test_denial_stops_before_payment(self):
        """A failed permission wait produces a blocked result with no effect."""
        self.workflow.denied = True
        result = await self._invoke()
        self.assertEqual(result, {"status": "blocked"})
        self.assertEqual(self.calls, [
            "human_approval", "open", "approval", "wait:refund_issued",
        ])

    async def test_engine_unavailable_stops_before_payment(self):
        """A failed live-state lookup stays distinct from a contract denial."""
        self.workflow.unavailable = True
        result = await self._invoke()
        self.assertEqual(result, {"status": "threadify_unavailable"})
        self.assertEqual(self.calls, [
            "human_approval", "open", "approval", "wait:refund_issued",
        ])

    async def test_backend_reuses_receipt_and_checks_order_owner(self):
        """The local payment service enforces ownership and idempotency itself."""
        backend = _load("refund_backend_example", _ROOT / "backend.py")
        with patch.dict(os.environ, {"REFUND_API_TOKEN": "local-key"}):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=backend.app), base_url="http://localhost",
            ) as client:
                headers = {"Authorization": "Bearer local-key"}
                created = await client.post(
                    "/demo/orders", headers=headers,
                    json={"id": "O-42", "owner": "alice", "amount": 42, "currency": "GBP"},
                )
                self.assertEqual(created.status_code, 200)
                bound = await client.put(
                    "/demo/orders/O-42/thread", headers=headers,
                    json={"thread_id": "thread-42"},
                )
                self.assertEqual(bound.status_code, 200)
                repeated = await client.post(
                    "/demo/orders", headers=headers,
                    json={"id": "O-42", "owner": "alice", "amount": 42, "currency": "GBP"},
                )
                self.assertEqual(repeated.json()["thread_id"], "thread-42")
                denied = await client.get("/orders/O-42", headers={**headers, "X-Actor": "bob"})
                self.assertEqual(denied.status_code, 404)
                payload = {"order_id": "O-42", "amount": 42, "currency": "GBP"}
                request_headers = {**headers, "X-Actor": "alice", "Idempotency-Key": "grant-1"}
                first = await client.post("/refunds", headers=request_headers, json=payload)
                retry = await client.post("/refunds", headers=request_headers, json=payload)
                second = await client.post(
                    "/refunds", headers={**request_headers, "Idempotency-Key": "grant-2"},
                    json=payload,
                )
                self.assertEqual(first.json(), retry.json())
                self.assertEqual(second.status_code, 409)
