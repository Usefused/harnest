import unittest

from harnest.lifecycle import (
    DROP_EVENT,
    LifecycleContext,
    lifecycle,
    registration_for,
)


class ExtensionContractTests(unittest.TestCase):
    def test_direct_and_configured_decorators_attach_explicit_metadata(self):
        @lifecycle.before_invoke
        def before(_context, value):
            return value

        @lifecycle.after_invoke(order=20)
        async def after(_context, value):
            return value

        self.assertEqual(registration_for(before).phase, "before_invoke")
        self.assertEqual(registration_for(before).order, 0)
        self.assertEqual(registration_for(after).order, 20)
        self.assertEqual(repr(DROP_EVENT), "DROP_EVENT")

    def test_native_decorators_are_framework_specific(self):
        @lifecycle.adk_plugin(order=3)
        def adk():
            return object()

        @lifecycle.langgraph_middleware
        def langgraph():
            return object()

        self.assertEqual(registration_for(adk).phase, "adk_plugin")
        self.assertEqual(registration_for(adk).framework, "adk")
        self.assertEqual(
            registration_for(langgraph).phase, "langgraph_middleware"
        )

    def test_session_store_decorator_registers_a_root_factory(self):
        @lifecycle.session_store
        def sessions():
            return object()

        registration = registration_for(sessions)
        self.assertEqual(registration.phase, "session_store")
        self.assertIsNone(registration.framework)

    def test_decorator_validation_rejects_ambiguous_registration(self):
        with self.assertRaisesRegex(TypeError, "integer"):
            lifecycle.on_event(order=True)

        with self.assertRaisesRegex(TypeError, "only one"):

            @lifecycle.on_error
            @lifecycle.after_invoke
            def duplicated(_context, _value):
                return None

    def test_context_has_an_invocation_scoped_scratchpad(self):
        first = LifecycleContext(
            framework="langgraph",
            agent_name="support",
            invocation_id="invoke-1",
            user_id="user-1",
            session_id="session-1",
        )
        second = LifecycleContext(
            framework="langgraph",
            agent_name="support",
            invocation_id="invoke-2",
            user_id="user-1",
            session_id="session-1",
        )
        first.attributes["value"] = 1
        self.assertEqual(first.attributes, {"value": 1})
        self.assertEqual(second.attributes, {})

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
