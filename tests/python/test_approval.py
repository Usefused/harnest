import asyncio
import tempfile
import unittest
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from harnest.approval import (
    ApprovalChallenge,
    ApprovalEnforcementError,
    ApprovalExecution,
    ApprovalExpired,
    ApprovalPolicy,
    ApprovalRequired,
    InMemoryApprovalStore,
    _bound_arguments,
    approval_execution,
    require_human_approval,
)
from harnest.bundle import BundleExportError, BundleImportError, _discover_mcp
from harnest.tool import tool
from harnest.mcp import MCPClient


class ApprovalAuthoringTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_decorator_orders_both_enforce_before_execution(self):
        calls = []

        @tool
        @require_human_approval(message="Delete {customer_id}?")
        def inner(customer_id: str) -> str:
            """Delete one customer."""
            calls.append(customer_id)
            return customer_id

        @require_human_approval(message="Delete {customer_id}?")
        @tool
        def outer(customer_id: str) -> str:
            """Delete one customer."""
            calls.append(customer_id)
            return customer_id

        for protected in (inner, outer):
            with self.subTest(protected=protected.__name__):
                execution = ApprovalExecution("user", "session", "call")
                with approval_execution(execution), self.assertRaises(
                    ApprovalRequired
                ) as caught:
                    await protected("customer-7")
                self.assertEqual(caught.exception.challenge.action, f"tool:{protected.__name__}")
                self.assertEqual(caught.exception.challenge.message, "Delete customer-7?")
        self.assertEqual(calls, [])

    async def test_approval_is_bound_to_arguments_and_consumed_once(self):
        calls = []

        @tool
        @require_human_approval(message="Save {value}?")
        def save(value: str) -> str:
            """Save a value."""
            calls.append(value)
            return value

        invocation = object()
        execution = ApprovalExecution("user", "session", "call")
        with approval_execution(execution), self.assertRaises(ApprovalRequired) as caught:
            await save("original")
        store = InMemoryApprovalStore()
        pending = store.request(
            caught.exception.challenge,
            user_id="user",
            session_id="session",
            call_id="call",
            invocation=invocation,
        )
        store.decide(pending.id, user_id="user", decision="approve")

        with approval_execution(
            ApprovalExecution("user", "session", "call", grant=pending)
        ):
            self.assertEqual(await save("original"), "original")
            with self.assertRaisesRegex(ApprovalEnforcementError, "already consumed"):
                await save("original")
        store.assert_consumed(pending)
        self.assertEqual(calls, ["original"])

    async def test_approval_fails_closed_without_managed_runtime(self):
        @tool
        @require_human_approval(message="Approve?")
        def protected() -> None:
            """Perform protected work."""

        with self.assertRaisesRegex(ApprovalEnforcementError, "managed Harnest"):
            await protected()

    async def test_business_context_argument_is_bound(self):
        @tool
        @require_human_approval(message="Transfer?")
        async def transfer(context: str, amount: int) -> str:
            """Transfer an amount to the named context."""

            return f"{context}:{amount}"

        with approval_execution(ApprovalExecution("u", "s", "c")):
            with self.assertRaises(ApprovalRequired) as caught:
                await transfer("alice", 10)
        store = InMemoryApprovalStore()
        pending = store.request(
            caught.exception.challenge,
            user_id="u",
            session_id="s",
            call_id="c",
        )
        store.decide(pending.id, user_id="u", decision="approve")
        with approval_execution(ApprovalExecution("u", "s", "c", pending)):
            with self.assertRaisesRegex(ApprovalEnforcementError, "did not match"):
                await transfer("mallory", 10)

    def test_only_proven_framework_context_instances_are_excluded(self):
        contexts = []
        try:
            from google.adk.tools.tool_context import ToolContext

            contexts.append(ToolContext.__new__(ToolContext))
        except ImportError:
            pass
        try:
            from langgraph.prebuilt.tool_node import ToolRuntime

            contexts.append(ToolRuntime.__new__(ToolRuntime))
        except ImportError:
            pass
        for context in contexts:
            with self.subTest(context=type(context).__name__):
                self.assertEqual(
                    _bound_arguments({"renamed": context, "amount": 10}),
                    {"amount": 10},
                )

    async def test_denied_and_expired_grants_never_authorize(self):
        @tool
        @require_human_approval(message="Run?")
        async def protected(value: str) -> str:
            """Return one protected value."""

            return value

        with approval_execution(ApprovalExecution("u", "s", "c")):
            with self.assertRaises(ApprovalRequired) as caught:
                await protected("value")
        now = [time.time()]
        store = InMemoryApprovalStore(clock=lambda: now[0])
        denied = store.request(
            caught.exception.challenge, user_id="u", session_id="s", call_id="c"
        )
        store.decide(denied.id, user_id="u", decision="deny")
        with approval_execution(ApprovalExecution("u", "s", "c", denied)):
            with self.assertRaisesRegex(ApprovalEnforcementError, "not approved"):
                await protected("value")

        expired = store.request(
            caught.exception.challenge, user_id="u", session_id="s", call_id="c"
        )
        store.decide(expired.id, user_id="u", decision="approve")
        now[0] = expired.expires_at
        with approval_execution(ApprovalExecution("u", "s", "c", expired)):
            with self.assertRaisesRegex(ApprovalEnforcementError, "expired"):
                await protected("value")

    def test_store_cleanup_is_bounded_and_drops_terminal_items(self):
        policy = ApprovalPolicy(message="Approve?")
        from harnest.approval import ApprovalChallenge

        challenge = ApprovalChallenge("tool:test", "hash", "Approve?", policy)
        store = InMemoryApprovalStore(max_pending=1, tombstone_seconds=0)
        pending = store.request(challenge, user_id="u", session_id="s", call_id="c")
        with self.assertRaisesRegex(ApprovalEnforcementError, "capacity"):
            store.request(challenge, user_id="u", session_id="s", call_id="other")
        store.decide(pending.id, user_id="u", decision="deny")
        self.assertEqual(store.retained_count, 0)

    async def test_concurrent_decisions_have_one_atomic_winner(self):
        policy = ApprovalPolicy(message="Approve?")
        store = InMemoryApprovalStore()
        pending = store.request(
            ApprovalChallenge("tool:test", "hash", "Approve?", policy),
            user_id="u",
            session_id="s",
            call_id="c",
        )

        def decide(value):
            try:
                return store.decide(pending.id, user_id="u", decision=value).status
            except ApprovalEnforcementError as exc:
                return type(exc).__name__

        outcomes = await asyncio.gather(
            asyncio.to_thread(decide, "approve"),
            asyncio.to_thread(decide, "deny"),
        )
        self.assertEqual(sum(item in {"approved", "denied"} for item in outcomes), 1)
        self.assertEqual(outcomes.count("ApprovalEnforcementError"), 1)

    async def test_decision_at_expiry_wakes_and_cleans_suspended_task(self):
        now = [100.0]
        store = InMemoryApprovalStore(clock=lambda: now[0], tombstone_seconds=0)
        run = store.create_run(user_id="u", session_id="s", call_id="c")
        challenge = ApprovalChallenge(
            "tool:test",
            "hash",
            "Approve?",
            ApprovalPolicy(message="Approve?", timeout_seconds=1),
        )
        suspended = asyncio.create_task(store.suspend(run, challenge))
        kind, pending = await run.notifications.get()
        self.assertEqual(kind, "approval")
        now[0] = pending.expires_at

        with self.assertRaisesRegex(ApprovalEnforcementError, "expired"):
            store.decide(pending.id, user_id="u", decision="approve")
        with self.assertRaises(ApprovalExpired):
            await suspended
        self.assertEqual(store.retained_count, 0)

    def test_audit_excludes_payloads_and_unique_request_ids(self):
        policy = ApprovalPolicy(message="secret rendered message")
        challenge = ApprovalChallenge(
            "tool:stable_capability", "secret-argument-hash", policy.message, policy
        )
        store = InMemoryApprovalStore()
        audit = Mock()
        with patch("harnest.approval._AUDIT", SimpleNamespace(info=audit)):
            store.request(
                challenge,
                user_id="secret-user",
                session_id="secret-session",
                call_id="secret-call",
            )
        _event, kwargs = audit.call_args.args[0], audit.call_args.kwargs
        self.assertEqual(kwargs["action"], "tool:stable_capability")
        rendered = repr(audit.call_args)
        for secret in (
            "secret-argument-hash",
            "secret rendered message",
            "secret-user",
            "secret-session",
            "secret-call",
            "approval_",
        ):
            self.assertNotIn(secret, rendered)

    def test_mcp_factory_uses_filename_identity_and_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "github.py").write_text(
                "import os\n"
                "from harnest.approval import require_human_approval\n"
                "from harnest.mcp import MCPClient\n"
                "@require_human_approval(\n"
                "    tools=['merge_pull_request'], message='Approve GitHub write?')\n"
                "def client():\n"
                "    return MCPClient.streamable_http(os.environ['MCP_URL'])\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"MCP_URL": "https://mcp.test"}):
                clients = _discover_mcp(directory)
        self.assertEqual(clients[0].identity, "github")
        self.assertEqual(clients[0].url, "https://mcp.test")
        self.assertEqual(clients[0].approval.tools, ("merge_pull_request",))

    def test_mcp_requires_fixed_zero_argument_factory(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "legacy.py").write_text(
                "from harnest.mcp import MCPClient\n"
                "legacy = MCPClient.sse('https://mcp.test/sse')\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(BundleExportError, "export 'client'"):
                _discover_mcp(directory)

            (directory / "legacy.py").write_text(
                "def client(required):\n    return required\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(BundleExportError, "accept no arguments"):
                _discover_mcp(directory)

            (directory / "legacy.py").write_text(
                "def client():\n    raise TypeError('credential-secret')\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(BundleImportError, "failed with TypeError") as caught:
                _discover_mcp(directory)
            self.assertNotIn("credential-secret", str(caught.exception))

            (directory / "legacy.py").write_text(
                "from harnest.mcp import MCPClient\n"
                "def client():\n    return MCPClient.sse('https://mcp.test/sse')\n"
                "def second_client():\n    return client()\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(BundleExportError, "additional MCP client"):
                _discover_mcp(directory)


class AsyncApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_tool_executes_only_with_matching_grant(self):
        @tool
        @require_human_approval(message="Send {value}?")
        async def send(value: str) -> str:
            """Send a value."""
            await asyncio.sleep(0)
            return value

        with approval_execution(ApprovalExecution("u", "s", "c")), self.assertRaises(
            ApprovalRequired
        ) as caught:
            await send("one")
        store = InMemoryApprovalStore()
        pending = store.request(
            caught.exception.challenge,
            user_id="u",
            session_id="s",
            call_id="c",
            invocation=object(),
        )
        store.decide(pending.id, user_id="u", decision="approve")
        with approval_execution(ApprovalExecution("u", "s", "c", pending)):
            self.assertEqual(await send("one"), "one")

    async def test_adk_mcp_gate_filters_remote_tools_before_execution(self):
        policy_decorator = require_human_approval(
            tools=["merge"], message="Approve merge?"
        )

        @policy_decorator
        def factory():
            return MCPClient.sse("https://mcp.test/sse")

        client = factory()
        from dataclasses import replace
        from harnest.approval import approval_policy

        client = replace(client, identity="github", approval=approval_policy(factory))

        class Toolset:
            def __init__(self, **_kwargs):
                pass

            async def get_tools(self, _readonly_context=None):
                async def run_async(*, args, tool_context):
                    return {"args": args, "context": tool_context}

                return [
                    SimpleNamespace(name="merge", run_async=run_async),
                    SimpleNamespace(name="read", run_async=run_async),
                ]

        guarded_type = client._adk_toolset_type(Toolset)
        tools = await guarded_type().get_tools()
        self.assertNotEqual(tools[0].run_async, tools[1].run_async)
        with approval_execution(ApprovalExecution("u", "s", "c")), self.assertRaises(
            ApprovalRequired
        ) as caught:
            await tools[0].run_async(
                args={"repository": "harnest"}, tool_context=object()
            )
        self.assertEqual(caught.exception.challenge.action, "mcp:github.merge")

    async def test_langgraph_mcp_middleware_gates_selected_call(self):
        middleware_module = ModuleType("langchain.agents.middleware")
        middleware_module.wrap_tool_call = lambda function: function
        agents_module = ModuleType("langchain.agents")
        agents_module.__path__ = []
        langchain_module = ModuleType("langchain")
        langchain_module.__path__ = []
        policy = ApprovalPolicy(message="Approve merge?", tools=("merge",))
        modules = {
            "langchain": langchain_module,
            "langchain.agents": agents_module,
            "langchain.agents.middleware": middleware_module,
        }
        with patch.dict(sys.modules, modules):
            from harnest.backends.langgraph import mcp_approval_middleware

            middleware = mcp_approval_middleware(
                {"github_merge": ("github", "merge", policy)}
            )
        request = SimpleNamespace(
            tool_call={
                "name": "github_merge",
                "args": {"repository": "harnest"},
            }
        )

        async def handler(_request):
            return "merged"

        with approval_execution(ApprovalExecution("u", "s", "c")), self.assertRaises(
            ApprovalRequired
        ) as caught:
            await middleware(request, handler)
        store = InMemoryApprovalStore()
        pending = store.request(
            caught.exception.challenge,
            user_id="u",
            session_id="s",
            call_id="c",
            invocation=object(),
        )
        store.decide(pending.id, user_id="u", decision="approve")
        with approval_execution(ApprovalExecution("u", "s", "c", pending)):
            self.assertEqual(await middleware(request, handler), "merged")


if __name__ == "__main__":
    unittest.main()
