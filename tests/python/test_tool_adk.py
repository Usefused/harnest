from dataclasses import replace
from types import SimpleNamespace
import unittest

from harnest.context import (
    activate_context,
    context,
    create_agent_context,
    revoke_context,
)
from harnest.lifecycle import LifecycleListener
from harnest.mcp_context import _mark_governed_mcp_operation
from harnest.tool_adk import ADKPortableToolLifecyclePlugin


def _listener(phase, callback, *, order=0, name="hook"):
    return LifecycleListener(phase, callback, order, f"{name}.py", 1, name)


def _context():
    return create_agent_context(
        framework="adk",
        agent_name="root",
        invocation_id="invoke-1",
        user_id="user-1",
        session_id="session-1",
        metadata={},
        resources={},
    )


class ADKPortableToolLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_tool_is_intercepted_once_with_subagent_identity(self):
        seen = []

        def before(lifecycle_context, request):
            seen.append(("before", lifecycle_context.agent_name, context.agent_name))
            return lifecycle_context.next(
                replace(request, kwargs={"value": 4})
            )

        def after(lifecycle_context, result):
            seen.append(("after", lifecycle_context.agent_name, context.agent_name))
            return lifecycle_context.next(result + 1)

        plugin = ADKPortableToolLifecyclePlugin(
            (
                _listener("before_tool", before),
                _listener("after_tool", after),
            )
        )
        tool = SimpleNamespace(name="native_lookup", run_async=object())
        tool_context = SimpleNamespace(agent_name="researcher")
        arguments = {"value": 1}
        active = _context()

        with activate_context(active):
            short = await plugin.before_tool_callback(
                tool=tool, tool_args=arguments, tool_context=tool_context
            )
            self.assertIsNone(short)
            result = await plugin.after_tool_callback(
                tool=tool,
                tool_args=arguments,
                tool_context=tool_context,
                result=arguments["value"] * 2,
            )

        revoke_context(active)
        self.assertEqual(arguments, {"value": 4})
        self.assertEqual(result, 9)
        self.assertEqual(
            seen,
            [
                ("before", "researcher", "researcher"),
                ("after", "researcher", "researcher"),
            ],
        )

    async def test_managed_function_and_mcp_marker_skip_native_layer(self):
        calls = []
        plugin = ADKPortableToolLifecyclePlugin(
            (
                _listener(
                    "before_tool",
                    lambda lifecycle_context, request: calls.append(request),
                ),
            )
        )

        async def managed_function():
            return None

        setattr(managed_function, "__harnest_tool_lifecycle_wrapped__", True)

        async def marker_operation(*, args, tool_context):
            del args, tool_context

        _mark_governed_mcp_operation(marker_operation)
        tools = (
            SimpleNamespace(
                name="managed", func=managed_function, run_async=object()
            ),
            SimpleNamespace(name="remote", run_async=marker_operation),
        )
        active = _context()

        with activate_context(active):
            for tool in tools:
                result = await plugin.before_tool_callback(
                    tool=tool,
                    tool_args={},
                    tool_context=SimpleNamespace(agent_name="root"),
                )
                self.assertIsNone(result)

        revoke_context(active)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
