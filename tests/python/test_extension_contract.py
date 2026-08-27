import unittest

from harnest.extension import DROP_EVENT, Extension, LifecycleContext


class ExtensionContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_portable_hooks_are_explicit(self):
        calls = []

        def before(context, request):
            context.attributes["started"] = True
            calls.append(("before", request))
            return {"input": "checked"}

        async def after(context, result):
            calls.append(("after", result, context.attributes["started"]))
            return {"output": "guarded"}

        extension = Extension(
            name="guardrails",
            before_invoke=before,
            after_invoke=after,
        )
        context = LifecycleContext(
            framework="adk",
            agent_name="support",
            invocation_id="invoke-1",
            user_id="user-1",
            session_id="session-1",
        )

        self.assertEqual(
            extension.before_invoke(context, {"input": "raw"}),
            {"input": "checked"},
        )
        after_hook = extension.after_invoke
        self.assertIsNotNone(after_hook)
        assert after_hook is not None
        self.assertEqual(
            await after_hook(context, {"output": "raw"}),
            {"output": "guarded"},
        )
        self.assertEqual(
            calls,
            [
                ("before", {"input": "raw"}),
                ("after", {"output": "raw"}, True),
            ],
        )

    async def test_event_transforms_and_error_is_notification_only(self):
        errors = []

        def on_event(_context, event):
            return {**event, "stored": True}

        async def on_error(_context, error):
            errors.append(str(error))

        extension = Extension(
            name="bigquery_history",
            on_event=on_event,
            on_error=on_error,
        )
        context = LifecycleContext(
            framework="langgraph",
            agent_name="support",
            invocation_id="invoke-1",
            user_id="user-1",
            session_id="session-1",
        )

        event_hook = extension.on_event
        error_hook = extension.on_error
        assert event_hook is not None
        assert error_hook is not None
        self.assertEqual(
            event_hook(context, {"type": "message"}),
            {"type": "message", "stored": True},
        )
        self.assertIsNone(await error_hook(context, RuntimeError("failed")))
        self.assertEqual(errors, ["failed"])
        self.assertEqual(repr(DROP_EVENT), "DROP_EVENT")

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

    def test_contract_validation_is_strict(self):
        with self.assertRaisesRegex(ValueError, "extension name"):
            Extension(name="bad-name")
        with self.assertRaisesRegex(TypeError, "before_invoke"):
            Extension(name="guardrails", before_invoke=object())
        with self.assertRaisesRegex(ValueError, "unsupported extension framework"):
            LifecycleContext(
                framework="other",
                agent_name="support",
                invocation_id="invoke-1",
                user_id="user-1",
                session_id="session-1",
            )


if __name__ == "__main__":
    unittest.main()
