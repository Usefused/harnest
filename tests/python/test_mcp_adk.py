from dataclasses import replace
from types import SimpleNamespace
import unittest

from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents import BaseAgent
from google.adk.sessions import Session
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.tools.tool_context import ToolContext

from harnest.context import (
    activate_context,
    context,
    create_agent_context,
    revoke_context,
)
from harnest.lifecycle import LifecycleListener
from harnest.mcp_adk import (
    _discover_adk_mcp_clients,
    adk_mcp_context_plugins,
)
from harnest.mcp_context import (
    MCPContextUnavailableError,
    _activate_mcp_context,
)
from harnest.tool_lifecycle import tool_lifecycle_scope


def _listener(phase, callback, *, order=0, name="hook"):
    return LifecycleListener(phase, callback, order, f"{name}.py", 1, name)


def _agent_context():
    return create_agent_context(
        framework="adk",
        agent_name="root",
        invocation_id="harnest-invocation",
        user_id="user-1",
        session_id="session-1",
        metadata={},
        resources={},
    )


def _native_invocation():
    session = Session(
        id="session-1",
        app_name="root",
        user_id="user-1",
        state={},
        events=[],
    )
    return InvocationContext(
        session_service=InMemorySessionService(),
        invocation_id="adk-invocation",
        agent=BaseAgent(name="root"),
        session=session,
    )


class _Toolset:
    __harnest_mcp_public_name__ = "billing"
    __harnest_mcp_client_name__ = "mcp__billing"
    __harnest_mcp_approval_wrapped__ = True
    tool_name_prefix = "remote"

    def __init__(self, tool):
        self.tool = tool

    async def get_tools_with_prefix(self, _readonly_context):
        return [self.tool]


class ADKMCPContextAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_and_facade_calls_share_one_governed_marker(self):
        calls = []
        phases = []

        async def operation(*, args, tool_context):
            calls.append((dict(args), tool_context))
            return {"value": args["value"]}

        remote = SimpleNamespace(name="remote_echo", run_async=operation)
        target = SimpleNamespace(
            tools=[_Toolset(remote)], sub_agents=[], graph=None
        )
        invocation = _native_invocation()
        active = _agent_context()

        def before_mcp(lifecycle_context, request):
            phases.append(("mcp", lifecycle_context.client_name))
            return lifecycle_context.next(
                replace(
                    request,
                    arguments={"value": request.arguments["value"] + 1},
                )
            )

        def before_tool(lifecycle_context, request):
            phases.append(("tool", lifecycle_context.tool_name))
            return lifecycle_context.next()

        mcp_listeners = (_listener("before_mcp", before_mcp),)
        tool_listeners = (_listener("before_tool", before_tool),)

        with activate_context(active):
            clients = await _discover_adk_mcp_clients(target, invocation)
            with _activate_mcp_context(
                clients, mcp_listeners
            ), tool_lifecycle_scope(tool_listeners):
                native_context = SimpleNamespace(agent_name="root")
                native = await remote.run_async(
                    args={"value": 1}, tool_context=native_context
                )
                facade = await context.mcp("billing").call_tool(
                    "echo", {"value": 3}
                )

        revoke_context(active)
        self.assertEqual(native, {"value": 2})
        self.assertEqual(facade, {"value": 4})
        self.assertEqual([item[0] for item in calls], [{"value": 2}, {"value": 4}])
        self.assertIs(calls[0][1], native_context)
        self.assertIsInstance(calls[1][1], ToolContext)
        self.assertEqual(
            phases,
            [
                ("mcp", "billing"),
                ("tool", "echo"),
                ("mcp", "billing"),
                ("tool", "echo"),
            ],
        )

    async def test_plugin_revokes_retained_facade_after_run(self):
        async def operation(*, args, tool_context):
            del tool_context
            return args

        remote = SimpleNamespace(name="remote_echo", run_async=operation)
        target = SimpleNamespace(
            tools=[_Toolset(remote)], sub_agents=[], graph=None
        )
        enter, exit_plugin = adk_mcp_context_plugins(target, ())
        invocation = _native_invocation()
        active = _agent_context()

        with activate_context(active):
            await enter.before_run_callback(invocation_context=invocation)
            retained = context.mcp("billing")
            self.assertEqual(await retained.call_tool("echo", {}), {})
            await exit_plugin.after_run_callback(invocation_context=invocation)
            with self.assertRaises(MCPContextUnavailableError):
                await retained.call_tool("echo", {})

        revoke_context(active)

    async def test_duplicate_public_identity_fails_materialization(self):
        first = SimpleNamespace(name="remote_first", run_async=lambda **_kwargs: None)
        second = SimpleNamespace(name="remote_second", run_async=lambda **_kwargs: None)
        target = SimpleNamespace(
            tools=[_Toolset(first), _Toolset(second)],
            sub_agents=[],
            graph=None,
        )

        with self.assertRaisesRegex(ValueError, "duplicate public identity"):
            await _discover_adk_mcp_clients(target, _native_invocation())

    async def test_cached_remote_tool_gets_a_fresh_invocation_marker(self):
        calls = []

        async def operation(*, args, tool_context):
            del tool_context
            calls.append(dict(args))
            return args

        remote = SimpleNamespace(name="remote_echo", run_async=operation)
        target = SimpleNamespace(
            tools=[_Toolset(remote)], sub_agents=[], graph=None
        )
        active = _agent_context()

        with activate_context(active):
            for value in (1, 2):
                clients = await _discover_adk_mcp_clients(
                    target, _native_invocation()
                )
                with _activate_mcp_context(clients):
                    result = await remote.run_async(
                        args={"value": value},
                        tool_context=SimpleNamespace(agent_name="root"),
                    )
                    self.assertEqual(result, {"value": value})

        revoke_context(active)
        self.assertEqual(calls, [{"value": 1}, {"value": 2}])


if __name__ == "__main__":
    unittest.main()
