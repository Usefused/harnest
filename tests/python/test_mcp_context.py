import asyncio
from dataclasses import replace
from types import MappingProxyType
import unittest
from unittest.mock import patch

from harnest.approval import ApprovalEnforcementError, ApprovalPolicy
from harnest.context import activate_context, create_agent_context
from harnest.lifecycle import LifecycleListener
from harnest.mcp import MCPClient, _adk_mcp_toolset_metadata
from harnest.mcp_context import (
    MCPClientUnavailableError,
    MCPContextUnavailableError,
    MCPLifecycleError,
    MCPToolCallError,
    MCPToolCallRequest,
    MCPToolUnavailableError,
    _activate_mcp_context,
    _managed_mcp_tool,
    mcp,
)
from harnest.tool_lifecycle import tool_lifecycle_scope


def _invocation():
    return create_agent_context(
        framework="langgraph",
        agent_name="support",
        invocation_id="run-mcp",
        user_id="user-1",
        session_id="session-1",
        metadata={},
        resources={},
    )


def _listener(phase, callback, *, order=0, line=1):
    return LifecycleListener(
        phase=phase,
        callback=callback,
        order=order,
        relative_path="extensions/mcp.py",
        line=line,
        function_name=getattr(callback, "__name__", "callback"),
    )


class MCPInvocationContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_adk_toolset_attaches_metadata_without_approval_subclass(self):
        class Recording:
            def __init__(self, *_args, **kwargs):
                self.kwargs = kwargs

        client = replace(
            MCPClient.sse("https://mcp.invalid/sse"),
            identity="billing",
            capability_id="mcp__billing",
        )
        with patch(
            "harnest.mcp._adk_mcp_classes",
            return_value=(Recording, Recording, Recording, Recording, Recording),
        ):
            toolset = client.to_adk_toolset()

        self.assertEqual(
            _adk_mcp_toolset_metadata(toolset),
            ("billing", "mcp__billing", False),
        )

    async def test_adk_metadata_separates_public_and_governance_names(self):
        class Toolset:
            async def get_tools(self, _readonly_context=None):
                return []

        client = replace(
            MCPClient.sse("https://mcp.invalid/sse"),
            identity="billing",
            capability_id="plugin__finance__mcp__billing",
            approval=ApprovalPolicy("Approve billing tool"),
        )
        toolset = client._adk_toolset_type(Toolset)()

        self.assertEqual(
            _adk_mcp_toolset_metadata(toolset),
            ("billing", "plugin__finance__mcp__billing", True),
        )

    async def test_named_client_dispatches_only_pre_governed_tools(self):
        received = []

        async def governed(arguments):
            received.append(arguments)
            return {"invoiceId": "invoice-1"}

        tool = _managed_mcp_tool("mcp__billing", "create_invoice", governed)
        with activate_context(_invocation()), _activate_mcp_context(
            {"billing": {"create_invoice": tool}}
        ), self.assertLogs("harnest.agent.mcp.audit", level="INFO") as captured:
            client = mcp("billing")
            result = await client.call_tool(
                "create_invoice", {"customer": "private-customer"}
            )

        self.assertEqual(result, {"invoiceId": "invoice-1"})
        self.assertIsInstance(received[0], MappingProxyType)
        self.assertEqual(captured.records[0].client, "mcp__billing")
        self.assertEqual(captured.records[0].tool, "create_invoice")
        self.assertEqual(captured.records[0].outcome, "committed")
        self.assertNotIn("private-customer", repr(captured.records[0].__dict__))
        self.assertFalse(hasattr(client, "transport"))
        self.assertFalse(hasattr(client, "credentials"))

    async def test_context_fails_closed_for_unknown_and_raw_tools(self):
        async def operation(arguments):
            return arguments

        with activate_context(_invocation()):
            with self.assertRaisesRegex(TypeError, "Harnest governed adapters"):
                with _activate_mcp_context(
                    {"billing": {"unsafe": operation}}  # type: ignore[dict-item]
                ):
                    pass
            with _activate_mcp_context(
                {
                    "billing": {
                        "safe": _managed_mcp_tool(
                            "mcp__billing", "safe", operation
                        )
                    }
                }
            ):
                with self.assertRaises(MCPClientUnavailableError):
                    mcp("missing")
                with self.assertRaises(MCPToolUnavailableError):
                    await mcp.client("billing").call_tool("missing")

    async def test_retained_and_child_task_bindings_are_revoked(self):
        release = asyncio.Event()

        async def operation(arguments):
            return arguments

        async def late_call(client):
            await release.wait()
            return await client.call_tool("safe")

        async def late_native_call(marker):
            await release.wait()
            return await marker.invoke({})

        with activate_context(_invocation()):
            marker = _managed_mcp_tool("mcp__billing", "safe", operation)
            with _activate_mcp_context(
                {"billing": {"safe": marker}}
            ):
                retained = mcp("billing")
                late = asyncio.create_task(late_call(retained))
                late_native = asyncio.create_task(late_native_call(marker))
            release.set()
            with self.assertRaises(MCPContextUnavailableError):
                await late
            with self.assertRaises(MCPContextUnavailableError):
                await late_native
            with self.assertRaises(MCPContextUnavailableError):
                mcp("billing")

    async def test_mcp_and_tool_lifecycles_share_one_explicit_path(self):
        order = []

        def before_mcp(context, request):
            order.append("mcp-before")
            self.assertNotIn("secret", repr(context))
            self.assertEqual(context.client_name, "billing")
            return context.next(MCPToolCallRequest({"amount": 2}))

        def before_tool(context, request):
            order.append("tool-before")
            self.assertEqual(context.tool_name, "charge")
            return context.next()

        async def operation(arguments):
            order.append("native")
            return {"amount": arguments["amount"]}

        def after_tool(context, result):
            order.append("tool-after")
            return context.next({**result, "tool": True})

        def after_mcp(context, result):
            order.append("mcp-after")
            return context.finish({**result, "mcp": True})

        listeners = (
            _listener("before_mcp", before_mcp, line=1),
            _listener("before_tool", before_tool, line=2),
            _listener("after_tool", after_tool, line=3),
            _listener("after_mcp", after_mcp, line=4),
        )
        marker = _managed_mcp_tool("mcp__billing", "charge", operation)
        with activate_context(_invocation()), tool_lifecycle_scope(
            listeners
        ), _activate_mcp_context(
            {"billing": {"charge": marker}}, listeners
        ):
            result = await marker.invoke({"amount": 1, "secret": "hidden"})

        self.assertEqual(
            order,
            ["mcp-before", "tool-before", "native", "tool-after", "mcp-after"],
        )
        self.assertEqual(result, {"amount": 2, "tool": True, "mcp": True})

    async def test_before_finish_skips_tool_approval_and_native_operation(self):
        called = False

        async def operation(arguments):
            nonlocal called
            called = True
            return arguments

        def finish(context, request):
            return context.finish({"cached": True})

        listener = _listener("before_mcp", finish)
        marker = _managed_mcp_tool(
            "mcp__billing",
            "charge",
            operation,
            approval=ApprovalPolicy("Approve charge"),
        )
        with activate_context(_invocation()), _activate_mcp_context(
            {"billing": {"charge": marker}}, (listener,)
        ), self.assertLogs("harnest.agent.mcp.audit", level="INFO") as captured:
            result = await mcp("billing").call_tool("charge")

        self.assertEqual(result, {"cached": True})
        self.assertFalse(called)
        self.assertEqual(captured.records[0].outcome, "finished")

    async def test_universal_tool_finish_is_not_audited_as_remote_commit(self):
        called = False

        async def operation(arguments):
            nonlocal called
            called = True
            return arguments

        def finish(context, request):
            return context.finish("local-result")

        listener = _listener("before_tool", finish)
        marker = _managed_mcp_tool("mcp__billing", "charge", operation)
        with activate_context(_invocation()), tool_lifecycle_scope(
            (listener,)
        ), _activate_mcp_context(
            {"billing": {"charge": marker}}
        ), self.assertLogs("harnest.agent.mcp.audit", level="INFO") as captured:
            result = await marker.invoke({})

        self.assertEqual(result, "local-result")
        self.assertFalse(called)
        self.assertEqual(captured.records[0].outcome, "finished")

    async def test_adapter_error_result_is_audited_as_failed_without_payload(self):
        async def operation(arguments):
            return {"isError": True, "content": "private-provider-body"}

        marker = _managed_mcp_tool("mcp__billing", "charge", operation)
        with activate_context(_invocation()), _activate_mcp_context(
            {"billing": {"charge": marker}}
        ), self.assertLogs("harnest.agent.mcp.audit", level="INFO") as captured:
            result = await marker.invoke({})

        self.assertTrue(result["isError"])
        self.assertEqual(captured.records[0].outcome, "failed")
        self.assertNotIn("private-provider-body", repr(captured.records[0].__dict__))

    async def test_hooks_require_explicit_flow_and_can_recover_safe_errors(self):
        observed = []

        async def operation(arguments):
            raise RuntimeError("https://private.invalid token=secret")

        def invalid_before(context, request):
            return None

        invalid = _managed_mcp_tool("mcp__billing", "invalid", operation)
        with activate_context(_invocation()), _activate_mcp_context(
            {"billing": {"invalid": invalid}},
            (_listener("before_mcp", invalid_before),),
        ):
            with self.assertRaises(MCPLifecycleError):
                await invalid.invoke({})

        def recover(context, error):
            observed.append(error)
            return context.finish("fallback")

        marker = _managed_mcp_tool("mcp__billing", "charge", operation)
        with activate_context(_invocation()), _activate_mcp_context(
            {"billing": {"charge": marker}},
            (_listener("on_mcp_error", recover),),
        ):
            result = await marker.invoke({"customer": "private"})

        self.assertEqual(result, "fallback")
        self.assertIsInstance(observed[0], MCPToolCallError)
        self.assertNotIn("private.invalid", str(observed[0]))
        self.assertIsNone(observed[0].__context__)

    async def test_error_hook_cannot_recover_missing_approval_runtime(self):
        called = False

        async def operation(arguments):
            nonlocal called
            called = True
            return arguments

        def unsafe_recovery(context, error):
            self.assertNotIn("Approve private", str(error))
            return context.finish("bypass")

        marker = _managed_mcp_tool(
            "mcp__billing",
            "charge",
            operation,
            approval=ApprovalPolicy("Approve private {customer}"),
        )
        with activate_context(_invocation()), _activate_mcp_context(
            {"billing": {"charge": marker}},
            (_listener("on_mcp_error", unsafe_recovery),),
        ):
            with self.assertRaises(ApprovalEnforcementError):
                await marker.invoke({"customer": "secret"})

        self.assertFalse(called)


if __name__ == "__main__":
    unittest.main()
