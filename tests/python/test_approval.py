import asyncio
import tempfile
import unittest
import time
from pathlib import Path
from types import SimpleNamespace
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
    request_human_approval,
    require_human_approval,
)
from harnest.bundle import BundleExportError, BundleImportError, _discover_mcp
from harnest.client_tool import client_tool
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

    async def test_client_tool_decorator_orders_both_require_approval_first(self):
        @client_tool
        @require_human_approval(message="Inspect {target}?")
        def inner(target: str) -> dict[str, str]:
            """Inspect a target in the connected client."""

            raise AssertionError("client tool bodies are declarations")

        @require_human_approval(message="Inspect {target}?")
        @client_tool
        def outer(target: str) -> dict[str, str]:
            """Inspect a target in the connected client."""

            raise AssertionError("client tool bodies are declarations")

        for protected in (inner, outer):
            with self.subTest(protected=protected.__name__):
                execution = ApprovalExecution("user", "session", "call")
                with approval_execution(execution), self.assertRaises(
                    ApprovalRequired
                ) as caught:
                    await protected("page")
                self.assertEqual(
                    caught.exception.challenge.action,
                    f"tool:{protected.__name__}",
                )
                self.assertEqual(caught.exception.challenge.message, "Inspect page?")

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
    async def test_dynamic_approval_suspends_after_evaluation_and_resumes_block(self):
        calls = []
        store = InMemoryApprovalStore()
        run = store.create_run(user_id="u", session_id="s", call_id="c")

        async def execute_typescript() -> str:
            calls.append("evaluated")
            async with request_human_approval(
                action="typescript.execute",
                message="Execute with {capability}?",
                arguments={"capability": "network", "sourceHash": "sha256:test"},
            ):
                calls.append("executed")
                return "complete"

        execution = ApprovalExecution("u", "s", "c", store=store, run=run)
        with approval_execution(execution):
            task = asyncio.create_task(execute_typescript())
        kind, pending = await run.notifications.get()

        self.assertEqual(kind, "approval")
        self.assertEqual(calls, ["evaluated"])
        self.assertEqual(pending.action, "dynamic:typescript.execute")
        self.assertEqual(pending.message, "Execute with network?")
        store.decide(pending.id, user_id="u", decision="approve")

        self.assertEqual(await task, "complete")
        self.assertEqual(calls, ["evaluated", "executed"])
        self.assertEqual(pending.status, "executed")

    async def test_dynamic_approval_is_conditional_and_fails_closed(self):
        async def execute(*, risky: bool) -> str:
            if not risky:
                return "safe"
            async with request_human_approval(
                action="typescript.execute",
                message="Execute TypeScript?",
                arguments={"source": object()},
            ):
                return "executed"

        self.assertEqual(await execute(risky=False), "safe")
        with approval_execution(ApprovalExecution("u", "s", "c")):
            with self.assertRaisesRegex(
                ApprovalEnforcementError, "approval cannot bind argument type object"
            ):
                await execute(risky=True)
        with self.assertRaisesRegex(ApprovalEnforcementError, "managed Harnest"):
            async with request_human_approval(
                action="typescript.execute", message="Execute TypeScript?"
            ):
                pass

    async def test_dynamic_approval_records_failure_inside_protected_block(self):
        store = InMemoryApprovalStore()
        run = store.create_run(user_id="u", session_id="s", call_id="c")

        async def fail() -> None:
            async with request_human_approval(
                action="database.delete",
                message="Delete the selected rows?",
                arguments={"queryHash": "sha256:test"},
            ):
                raise RuntimeError("operation failed")

        execution = ApprovalExecution("u", "s", "c", store=store, run=run)
        with approval_execution(execution):
            task = asyncio.create_task(fail())
        _kind, pending = await run.notifications.get()
        store.decide(pending.id, user_id="u", decision="approve")

        with self.assertRaisesRegex(RuntimeError, "operation failed"):
            await task
        self.assertEqual(pending.status, "execution_failed")

    async def test_dynamic_approval_rejects_payload_shaped_action_names(self):
        with self.assertRaisesRegex(ValueError, "stable identifier"):
            async with request_human_approval(
                action="execute customer source", message="Execute?"
            ):
                pass

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

    async def test_langgraph_mcp_interceptor_gates_selected_call(self):
        from langchain_mcp_adapters.interceptors import MCPToolCallRequest

        policy = ApprovalPolicy(message="Approve merge?", tools=("merge",))
        from harnest.runtime_langgraph import mcp_approval_interceptor

        interceptor = mcp_approval_interceptor({"github": ("github", policy)})
        request = MCPToolCallRequest(
            server_name="github",
            name="merge",
            args={"repository": "harnest"},
        )

        async def handler(_request):
            return "merged"

        with approval_execution(ApprovalExecution("u", "s", "c")), self.assertRaises(
            ApprovalRequired
        ) as caught:
            await interceptor(request, handler)
        self.assertEqual(caught.exception.challenge.action, "mcp:github.merge")
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
            self.assertEqual(await interceptor(request, handler), "merged")
        self.assertEqual(pending.status, "executed")

    async def test_langgraph_mcp_interceptor_routes_each_server_policy(self):
        from harnest.runtime_langgraph import mcp_approval_interceptor

        interceptor = mcp_approval_interceptor(
            {
                "github": ("mcp__github", ApprovalPolicy(message="Approve all?")),
                "catalog": (
                    "mcp__catalog",
                    ApprovalPolicy(message="Approve write?", tools=("write",)),
                ),
            }
        )
        calls = []

        async def handler(request):
            calls.append((request.server_name, request.name))
            return "ok"

        unprotected = SimpleNamespace(
            server_name="catalog", name="read", args={"secret": "value"}
        )
        unknown = SimpleNamespace(server_name="other", name="write", args={})
        self.assertEqual(await interceptor(unprotected, handler), "ok")
        self.assertEqual(await interceptor(unknown, handler), "ok")

        for server_name, tool_name, action in (
            ("github", "read", "mcp:mcp__github.read"),
            ("catalog", "write", "mcp:mcp__catalog.write"),
        ):
            request = SimpleNamespace(
                server_name=server_name, name=tool_name, args={"value": 1}
            )
            with approval_execution(ApprovalExecution("u", "s", "c")), self.assertRaises(
                ApprovalRequired
            ) as caught:
                await interceptor(request, handler)
            self.assertEqual(caught.exception.challenge.action, action)

        self.assertEqual(calls, [("catalog", "read"), ("other", "write")])

    async def test_langgraph_mcp_interceptor_audits_error_results_and_exceptions(self):
        from harnest.runtime_langgraph import mcp_approval_interceptor

        interceptor = mcp_approval_interceptor(
            {"github": ("github", ApprovalPolicy(message="Approve?"))}
        )
        request = SimpleNamespace(server_name="github", name="merge", args={})

        async def error_result(_request):
            return SimpleNamespace(isError=True)

        pending, failure = await self._approved_mcp_call(
            interceptor, request, error_result
        )
        self.assertIsNone(failure)
        self.assertEqual(pending.status, "execution_failed")

        async def raises(_request):
            raise RuntimeError("transport failed")

        pending, failure = await self._approved_mcp_call(interceptor, request, raises)
        self.assertIsInstance(failure, RuntimeError)
        self.assertEqual(str(failure), "transport failed")
        self.assertEqual(pending.status, "execution_failed")

    async def _approved_mcp_call(self, interceptor, request, handler):
        execution = ApprovalExecution("u", "s", "c")
        with approval_execution(execution), self.assertRaises(ApprovalRequired) as caught:
            await interceptor(request, handler)
        store = InMemoryApprovalStore()
        pending = store.request(
            caught.exception.challenge,
            user_id="u",
            session_id="s",
            call_id="c",
        )
        store.decide(pending.id, user_id="u", decision="approve")
        failure = None
        try:
            with approval_execution(ApprovalExecution("u", "s", "c", pending)):
                await interceptor(request, handler)
        except BaseException as exc:
            failure = exc
        return pending, failure


if __name__ == "__main__":
    unittest.main()
