"""Unhappy native evaluation paths must revoke managed invocation authority."""

import asyncio
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
import unittest

from google.adk.plugins import BasePlugin
from google.adk.agents import SequentialAgent
from google.adk.apps import App
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from harnest import context, tool
from harnest.context import ContextUnavailableError, optional_active_context
from harnest.credentials import Credential, CredentialProvider
from harnest.eval_adk import adk_evaluation_runtime
from harnest.eval_errors import EvaluationExecutionError
from harnest.evaluation import prepared_adk_eval_app
from harnest.runtime import _runtime_driver
from harnest.runtime_pipeline import start_runtime_pipeline
from test_eval_adk_context import _application, _eval_set, _evaluate


class _IdentityPlugin(BasePlugin):
    """Record native callback identity before cleanup can revoke it."""

    def __init__(self):
        super().__init__(name="identity_probe")
        self.seen = []

    def _record(self, phase, native):
        active = context.current()
        self.seen.append((phase, active, active.session_id, active.invocation_id,
                          native.session.id, native.invocation_id))

    async def before_run_callback(self, *, invocation_context):
        self._record("before", invocation_context)

    async def after_run_callback(self, *, invocation_context):
        self._record("after", invocation_context)

    async def on_run_error_callback(self, *, invocation_context, error):
        self._record("error", invocation_context)


class _CredentialProbe(CredentialProvider):
    """Verify evaluation owns provider lifetime and evaluator identity."""

    def __init__(self):
        self.events = []
        self.requests = []

    async def start(self):
        self.events.append("start")

    async def resolve(self, request):
        self.requests.append(request)
        return Credential("private-test-token")

    async def close(self):
        self.events.append("close")


class ADKEvaluationContextEdgeTests(unittest.TestCase):
    def _run(self, coroutine):
        """Suppress ADK's verbose reports while retaining assertion failures."""
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            return asyncio.run(coroutine)

    def _assert_revoked(self, plugin):
        """Callbacks must observe native identity without retaining authority."""
        self.assertTrue(plugin.seen)
        for _, active, session, invocation, native_session, native_invocation in plugin.seen:
            self.assertEqual(session, native_session)
            self.assertEqual(invocation, native_invocation)
            with self.assertRaises(ContextUnavailableError):
                active._require_active()
        self.assertIsNone(optional_active_context())

    def test_native_callbacks_keep_identity_until_success_cleanup(self):
        @tool
        def identity_tool() -> str:
            """Expose the current session only during native tool execution."""
            return context.session_id

        application = _application(identity_tool)
        plugin = _IdentityPlugin()
        application.native_app.plugins.append(plugin)
        self._run(_evaluate(application, _eval_set("identity_tool")))
        self.assertEqual([row[0] for row in plugin.seen], ["before", "after"])
        self.assertIs(plugin.seen[0][1], plugin.seen[1][1])
        self._assert_revoked(plugin)

    def test_context_error_has_actionable_redacted_diagnostic(self):
        @tool
        def unavailable_tool() -> str:
            """Simulate a retained or missing context in an authored tool."""
            raise ContextUnavailableError("private-provider-token")

        application = _application(unavailable_tool)
        plugin = _IdentityPlugin()
        application.native_app.plugins.append(plugin)
        with self.assertRaises(EvaluationExecutionError) as caught:
            self._run(_evaluate(application, _eval_set("unavailable_tool")))
        message = str(caught.exception)
        self.assertIn("list_skills", message)
        self.assertIn("managed evaluation entrypoint", message)
        self.assertIn("not a failed task-quality check", message)
        self.assertNotIn("private-provider-token", message)
        self.assertEqual([row[0] for row in plugin.seen], ["before", "error"])
        self._assert_revoked(plugin)

    def test_credential_provider_is_bound_and_closed_for_evaluation(self):
        provider = _CredentialProbe()

        @tool
        async def credentials_tool() -> str:
            """Resolve privately while asserting evaluator-owned identity."""
            value = await context.credentials.resolve("test-service", ["read"])
            self.assertEqual(value.reveal(), "private-test-token")
            self.assertEqual(provider.requests[-1].session_id, context.session_id)
            self.assertEqual(provider.requests[-1].invocation_id, context.invocation_id)
            self.assertEqual(provider.requests[-1].principal.user_id, context.user_id)
            return "resolved"

        application = _application(credentials_tool, credential_provider=provider)
        self._run(_evaluate(application, _eval_set("credentials_tool")))
        self.assertEqual(provider.events, ["start", "close"])
        self.assertEqual(len(provider.requests), 1)
        self.assertIsNone(optional_active_context())

    def test_native_generator_early_close_revokes_context(self):
        self._early_close(sequential=False)

    def test_sequential_generator_early_close_revokes_context(self):
        self._early_close(sequential=True)

    def _early_close(self, *, sequential):
        """Exercise both ADK node execution and its legacy workflow runner."""
        @tool
        def identity_tool() -> str:
            """Keep the model trajectory valid if it reaches tool execution."""
            return context.session_id

        plugin = _IdentityPlugin()
        application = self._execution_application(identity_tool, sequential)
        application.native_app.plugins.append(plugin)

        async def run():
            async with adk_evaluation_runtime(application):
                with prepared_adk_eval_app(application.native_app) as app:
                    runner, stream = await self._native_stream(app)
                    try:
                        await anext(stream)
                    finally:
                        await stream.aclose()
                        await runner.close()
            self.assertIsNone(optional_active_context())

        self._run(run())
        self._assert_revoked(plugin)

    def test_native_cancellation_revokes_context(self):
        self._cancel(sequential=False)

    def test_sequential_cancellation_revokes_context(self):
        self._cancel(sequential=True)

    def _cancel(self, *, sequential):
        """Cancel while a native tool owns the managed invocation context."""
        entered = asyncio.Event()
        plugin = _IdentityPlugin()

        @tool
        async def waiting_tool() -> str:
            """Pause inside real ADK dispatch until the consumer is cancelled."""
            entered.set()
            await asyncio.Future()
            return "unreachable"

        application = self._execution_application(waiting_tool, sequential)
        application.native_app.plugins.append(plugin)

        async def run():
            async with adk_evaluation_runtime(application):
                with prepared_adk_eval_app(application.native_app) as app:
                    runner, stream = await self._native_stream(app)

                    async def consume():
                        try:
                            async for _ in stream:
                                pass
                        finally:
                            await stream.aclose()
                            self._assert_revoked(plugin)

                    task = asyncio.create_task(consume())
                    try:
                        await asyncio.wait_for(entered.wait(), timeout=10)
                    finally:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                        await runner.close()
            self.assertIsNone(optional_active_context())

        self._run(run())
        self._assert_revoked(plugin)

    def _execution_application(self, function, sequential):
        """A sequential root selects ADK's distinct legacy callback lifecycle."""
        application = _application(function)
        if not sequential:
            return application
        root = SequentialAgent(name="workflow_probe", sub_agents=[application.target])
        return replace(application, name=root.name, target=root,
                       native_app=App(name=root.name, root_agent=root))

    def test_sequential_before_run_cancellation_revokes_context(self):
        """Cancellation can occur after context entry but before agent dispatch."""
        class CancelBeforeRun(_IdentityPlugin):
            async def before_run_callback(self, *, invocation_context):
                await super().before_run_callback(invocation_context=invocation_context)
                raise asyncio.CancelledError()

        @tool
        def identity_tool() -> str:
            """The callback must cancel before this function can run."""
            self.fail("tool should not run after callback cancellation")

        plugin = CancelBeforeRun()
        application = self._execution_application(identity_tool, sequential=True)
        application.native_app.plugins.append(plugin)

        async def run():
            async with adk_evaluation_runtime(application):
                with prepared_adk_eval_app(application.native_app) as app:
                    runner, stream = await self._native_stream(app)
                    try:
                        with self.assertRaises(asyncio.CancelledError):
                            await anext(stream)
                    finally:
                        await stream.aclose()
                        await runner.close()
                    self._assert_revoked(plugin)

        self._run(run())

    def test_playground_does_not_close_borrowed_native_plugin(self):
        """Only the live server owns the authored plugin's shutdown lifecycle."""
        class CloseProbe(_IdentityPlugin):
            def __init__(self):
                super().__init__()
                self.closed = 0

            async def close(self):
                self.closed += 1

        @tool
        def identity_tool() -> str:
            """Keep the borrowed evaluator run independent from model services."""
            return context.session_id

        plugin = CloseProbe()
        application = _application(identity_tool)
        application.native_app.plugins.append(plugin)

        async def run():
            driver = _runtime_driver(application)
            try:
                await start_runtime_pipeline(driver)
                async with adk_evaluation_runtime(application, driver):
                    with prepared_adk_eval_app(application.native_app) as app:
                        runner, stream = await self._native_stream(app)
                        try:
                            async for _ in stream:
                                pass
                        finally:
                            await stream.aclose()
                            await runner.close()
                self.assertEqual(plugin.closed, 0)
            finally:
                await driver.close()
            self.assertEqual(plugin.closed, 1)

        self._run(run())
        self._assert_revoked(plugin)

    def test_sequential_before_run_replacement_fails_closed(self):
        """Legacy early replacement has no reliable native generator exit hook."""
        class ReplaceBeforeRun(_IdentityPlugin):
            async def before_run_callback(self, *, invocation_context):
                await super().before_run_callback(invocation_context=invocation_context)
                return types.Content(role="model", parts=[types.Part(text="private-replacement")])

        @tool
        def identity_tool() -> str:
            """No model or tool may run after rejected early replacement."""
            self.fail("tool should not run after rejected callback replacement")

        plugin = ReplaceBeforeRun()
        application = self._execution_application(identity_tool, sequential=True)
        application.native_app.plugins.append(plugin)

        async def run():
            async with adk_evaluation_runtime(application):
                with prepared_adk_eval_app(application.native_app) as app:
                    runner, stream = await self._native_stream(app)
                    try:
                        with self.assertRaises(RuntimeError) as caught:
                            await anext(stream)
                    finally:
                        await stream.aclose()
                        await runner.close()
                    self.assertIn("before_run", str(caught.exception))
                    self.assertIn("return none", str(caught.exception).lower())
                    self.assertNotIn("private-replacement", str(caught.exception))
                    self._assert_revoked(plugin)

        self._run(run())

    def test_error_callback_failure_does_not_prevent_context_cleanup(self):
        """An authored error hook must not strand later internal cleanup hooks."""
        for sequential in (False, True):
            with self.subTest(sequential=sequential):
                self._error_callback_failure(sequential)

    def _error_callback_failure(self, sequential):
        """Check node and legacy runners both preserve original tool failures."""
        class BrokenErrorHook(_IdentityPlugin):
            async def on_run_error_callback(self, *, invocation_context, error):
                await super().on_run_error_callback(invocation_context=invocation_context, error=error)
                raise RuntimeError("private-error-hook-detail")

        @tool
        def broken_tool() -> str:
            """Trigger the native runner's authored error callback chain."""
            raise ValueError("original-tool-failure")

        plugin = BrokenErrorHook()
        application = self._execution_application(broken_tool, sequential=sequential)
        application.native_app.plugins.append(plugin)

        async def run():
            async with adk_evaluation_runtime(application):
                with prepared_adk_eval_app(application.native_app) as app:
                    runner, stream = await self._native_stream(app)
                    try:
                        with self.assertRaisesRegex(ValueError, "original-tool-failure"):
                            async for _ in stream:
                                pass
                    finally:
                        await stream.aclose()
                        await runner.close()
                    self._assert_revoked(plugin)

        self._run(run())
        self.assertEqual([row[0] for row in plugin.seen], ["before", "error"])

    async def _native_stream(self, app):
        """Use the same copied evaluation app with ADK-owned sessions/events."""
        sessions = InMemorySessionService()
        await sessions.create_session(app_name=app.name, user_id="eval-user",
                                      session_id="eval-session")
        runner = Runner(app=app, session_service=sessions)
        stream = runner.run_async(
            user_id="eval-user", session_id="eval-session",
            new_message=types.Content(role="user", parts=[types.Part(text="probe")]),
        )
        return runner, stream
