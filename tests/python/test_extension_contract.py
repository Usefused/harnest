import unittest

from harnest import lifecycle
from harnest.lifecycle import (
    DROP_EVENT,
    LifecycleContext,
    registration_for,
    registrations_for,
)


class ExtensionContractTests(unittest.TestCase):
    def test_decorators_preserve_callable_and_register_their_contract(self):
        """One metadata contract covers portable hooks and named/native factories."""
        cases = (
            (lifecycle.agent.before, "before_invoke", 0, None, None),
            (lifecycle.agent.after(order=20), "after_invoke", 20, None, None),
            (lifecycle.tool.before, "before_tool", 0, None, None),
            (lifecycle.http.before, "before_http", 0, None, None),
            (lifecycle.http.after(order=5), "after_http", 5, None, None),
            (lifecycle.model.before, "before_model", 0, None, None),
            (lifecycle.mcp.on_error, "on_mcp_error", 0, None, None),
            (lifecycle.adk_plugin(order=3), "adk_plugin", 3, "adk", None),
            (lifecycle.langgraph_middleware, "langgraph_middleware", 0, "langgraph", None),
            (lifecycle.storage.sessions, "session_store", 0, None, None),
            (lifecycle.storage.checkpoints, "checkpointer", 0, None, None),
            (lifecycle.storage.assets("default"), "asset_store", 0, None, "default"),
            (lifecycle.storage.assets(name="media"), "asset_store", 0, None, "media"),
            (lifecycle.skills.source("wex", order=-5), "skill_source", -5, None, "wex"),
            (lifecycle.output_policy, "output_policy", 0, None, None),
            (lifecycle.http_routes, "http_routes", 0, None, None),
            (lifecycle.resource(order=5), "resource", 5, None, None),
        )
        for decorator, phase, order, framework, name in cases:
            with self.subTest(phase=phase, name=name):
                def factory(*args):
                    return args

                async def async_factory(*args):
                    return args

                # Configured async hooks must retain coroutine identity too.
                target = async_factory if phase == "after_invoke" else factory
                self.assertIs(decorator(target), target)
                registration = registration_for(target)
                self.assertEqual(
                    (registration.phase, registration.order, registration.framework, registration.name),
                    (phase, order, framework, name),
                )

    def test_storage_namespace_allows_one_factory_to_fulfil_multiple_roles(self):
        """Keep shared connection ownership concise without allowing hook stacking."""

        @lifecycle.storage.sessions
        @lifecycle.storage.checkpoints
        @lifecycle.storage.custom("users")
        def state():
            return object()

        registrations = registrations_for(state)

        self.assertEqual(
            {item.phase for item in registrations},
            {"session_store", "checkpointer", "custom_store"},
        )
        custom = next(item for item in registrations if item.phase == "custom_store")
        self.assertEqual(custom.name, "users")

    def test_named_contributions_reject_unroutable_names(self):
        for decorator, name, error in (
            (lifecycle.storage.assets, "not/valid", "storage identifier"),
            (lifecycle.storage.custom, "", "storage identifier"),
            (lifecycle.skills.source, "not/valid", "source identifier"),
        ):
            with self.subTest(decorator=decorator, name=name), self.assertRaisesRegex(ValueError, error):
                decorator(name)

    def test_context_transitions_and_scratchpads_are_invocation_scoped(self):
        first = LifecycleContext("langgraph", "support", "invoke-1", "user-1", "session-1")
        second = LifecycleContext("langgraph", "support", "invoke-2", "user-1", "session-1")
        self.assertFalse(first.next().replaces)
        self.assertEqual(first.next("replacement").value, "replacement")
        self.assertEqual(first.finish("done").result, "done")
        self.assertEqual(repr(DROP_EVENT), "DROP_EVENT")
        first.attributes["value"] = 1
        self.assertEqual(first.attributes, {"value": 1})
        self.assertEqual(second.attributes, {})

    def test_decorator_validation_rejects_ambiguous_registration(self):
        with self.assertRaisesRegex(TypeError, "integer"):
            lifecycle.agent.on_event(order=True)

        with self.assertRaisesRegex(TypeError, "only one"):

            @lifecycle.agent.on_error
            @lifecycle.agent.after
            def duplicated(_context, _value):
                return None

    def test_context_validation_is_strict(self):
        with self.assertRaisesRegex(ValueError, "unsupported lifecycle framework"):
            LifecycleContext(
                framework="other",
                agent_name="support",
                invocation_id="invoke-1",
                user_id="user-1",
                session_id="session-1",
            )


if __name__ == "__main__":
    unittest.main()
