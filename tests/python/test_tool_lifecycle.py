from __future__ import annotations

from dataclasses import replace
import unittest

from harnest.context import activate_context, create_agent_context, revoke_context
from harnest.lifecycle import LifecycleListener
from harnest.tool_lifecycle import tool_lifecycle_scope, wrap_lifecycle_tool


def _listener(phase, callback, *, order=0, name="hook"):
    return LifecycleListener(phase, callback, order, f"{name}.py", 1, name)


def _context():
    return create_agent_context(
        framework="langgraph",
        agent_name="support",
        invocation_id="invoke-1",
        user_id="user-1",
        session_id="session-1",
        metadata={},
        resources={},
    )


class ToolLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_next_can_replace_arguments_and_result(self):
        seen = []

        def before(context, request):
            seen.append((context.tool_name, context.user_id))
            return context.next(replace(request, kwargs={"value": 4}))

        def after(context, result):
            return context.next(result + 1)

        async def implementation(*, value):
            return value * 2

        wrapped = wrap_lifecycle_tool(implementation)
        active = _context()
        try:
            with activate_context(active), tool_lifecycle_scope(
                (_listener("before_tool", before), _listener("after_tool", after))
            ):
                result = await wrapped(value=1)
        finally:
            revoke_context(active)

        self.assertEqual(result, 9)
        self.assertEqual(seen, [("implementation", "user-1")])

    async def test_finish_skips_tool_and_remaining_interceptors(self):
        called = []

        def stop(context, _request):
            return context.finish("cached")

        def later(context, _request):
            called.append("later")
            return context.next()

        async def implementation():
            called.append("tool")
            return "result"

        wrapped = wrap_lifecycle_tool(implementation)
        active = _context()
        try:
            with activate_context(active), tool_lifecycle_scope(
                (
                    _listener("before_tool", stop, name="stop"),
                    _listener("before_tool", later, order=10, name="later"),
                )
            ):
                result = await wrapped()
        finally:
            revoke_context(active)

        self.assertEqual(result, "cached")
        self.assertEqual(called, [])

    async def test_new_interceptors_reject_an_implicit_none_return(self):
        async def implementation():
            return "result"

        wrapped = wrap_lifecycle_tool(implementation)
        active = _context()
        try:
            with activate_context(active), tool_lifecycle_scope(
                (_listener("before_tool", lambda _context, _request: None),)
            ):
                with self.assertRaisesRegex(TypeError, "context.next"):
                    await wrapped()
        finally:
            revoke_context(active)


if __name__ == "__main__":
    unittest.main()
