import unittest

from harnest.lifecycle import (
    DROP_EVENT,
    LifecycleContext,
    lifecycle,
    registration_for,
    registrations_for,
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

    def test_checkpointer_decorator_registers_an_ownership_factory(self):
        @lifecycle.checkpointer
        def checkpoints():
            return object()

        registration = registration_for(checkpoints)

        self.assertIsNotNone(registration)
        self.assertEqual(registration.phase, "checkpointer")

    def test_asset_store_decorator_registers_a_root_factory(self):
        @lifecycle.asset_store
        def asset_store():
            return object()

        registration = registration_for(asset_store)
        self.assertIsNotNone(registration)
        self.assertEqual(registration.phase, "asset_store")
        self.assertIsNone(registration.framework)
        self.assertEqual(registration.name, "default")

    def test_asset_store_decorator_records_an_explicit_name(self):
        @lifecycle.asset_store(name="media")
        def media():
            return object()

        self.assertEqual(registration_for(media).name, "media")
        with self.assertRaisesRegex(ValueError, "storage identifier"):
            lifecycle.asset_store(name="not/valid")

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

    def test_storage_namespace_validates_named_contributions(self):
        """Reject names that cannot be routed consistently after compilation."""

        with self.assertRaisesRegex(ValueError, "storage identifier"):
            lifecycle.storage.assets("not/valid")
        with self.assertRaisesRegex(ValueError, "storage identifier"):
            lifecycle.storage.custom("")

    def test_skill_namespace_registers_ordered_named_sources(self):
        """Keep dynamic catalogs repeatable and independently routable."""

        @lifecycle.skills.source("wex", order=-5)
        def wex():
            return object()

        registration = registration_for(wex)

        self.assertEqual(registration.phase, "skill_source")
        self.assertEqual(registration.name, "wex")
        self.assertEqual(registration.order, -5)
        with self.assertRaisesRegex(ValueError, "source identifier"):
            lifecycle.skills.source("not/valid")

    def test_tool_and_agent_namespaces_map_to_portable_phases(self):
        """Offer cohesive namespaces while retaining existing flat hook phases."""

        @lifecycle.tool.before
        def before_tool(_context, value):
            return value

        @lifecycle.agent.after
        def after_agent(_context, value):
            return value

        self.assertEqual(registration_for(before_tool).phase, "before_tool")
        self.assertEqual(registration_for(after_agent).phase, "after_invoke")

    def test_http_namespace_maps_to_server_middleware_phases(self):
        """Keep HTTP interception cohesive and distinct from route factories."""

        @lifecycle.http.before
        def before_http(_context, value):
            return value

        @lifecycle.http.after(order=5)
        def after_http(_context, value):
            return value

        self.assertEqual(registration_for(before_http).phase, "before_http")
        self.assertEqual(registration_for(after_http).phase, "after_http")
        self.assertEqual(registration_for(after_http).order, 5)

    def test_model_and_mcp_namespaces_map_to_portable_phases(self):
        """Keep model and remote-tool stages discoverable without flat aliases."""

        @lifecycle.model.before
        def before_model(_context, value):
            return value

        @lifecycle.mcp.on_error
        def on_mcp_error(_context, error):
            return error

        self.assertEqual(registration_for(before_model).phase, "before_model")
        self.assertEqual(registration_for(on_mcp_error).phase, "on_mcp_error")

    def test_lifecycle_contexts_expose_explicit_transitions(self):
        """Let invocation listeners use the shared next/finish vocabulary."""

        context = LifecycleContext(
            framework="adk",
            agent_name="support",
            invocation_id="invoke-1",
            user_id="user-1",
            session_id="session-1",
        )

        self.assertFalse(context.next().replaces)
        self.assertEqual(context.next("replacement").value, "replacement")
        self.assertEqual(context.finish("done").result, "done")

    def test_output_policy_decorator_registers_an_optional_root_factory(self):
        @lifecycle.output_policy
        def output_policy():
            return object()

        registration = registration_for(output_policy)

        self.assertIsNotNone(registration)
        self.assertEqual(registration.phase, "output_policy")
        self.assertIsNone(registration.framework)

    def test_http_routes_decorator_registers_a_portable_root_factory(self):
        @lifecycle.http_routes
        def routes(_agent):
            return object()

        registration = registration_for(routes)

        self.assertIsNotNone(registration)
        self.assertEqual(registration.phase, "http_routes")
        self.assertIsNone(registration.framework)

    def test_resource_decorator_registers_a_runtime_only_factory(self):
        @lifecycle.resource(order=5)
        def vector_client():
            return object()

        registration = registration_for(vector_client)

        self.assertIsNotNone(registration)
        self.assertEqual(registration.phase, "resource")
        self.assertEqual(registration.order, 5)
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
