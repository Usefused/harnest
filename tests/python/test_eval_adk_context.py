"""Native ADK evaluation must run real managed tools, not only score canned text."""

import asyncio
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

from google.adk.apps import App
from google.adk.evaluation.agent_evaluator import AgentEvaluator
from google.adk.evaluation.eval_config import EvalConfig
from google.adk.evaluation.eval_set import EvalSet
from google.adk.models import BaseLlm, LlmResponse
from google.genai import types

from harnest import Agent, context, tool
from harnest.application import CompiledApplication
from harnest.bundle import EvalSuite
from harnest.context import ContextUnavailableError, ContextValue, optional_active_context
from harnest.eval_adk import adk_evaluation_runtime
from harnest.eval_errors import EvaluationExecutionError, evaluation_error_boundary
from harnest.evaluation import adk_eval_agent_module
from harnest.lifecycle import LifecycleListener
from harnest.runtime import _runtime_driver
from harnest.runtime_pipeline import start_runtime_pipeline
from harnest.session import InMemorySessionStore
from harnest.skills import SkillScope, create_skill_tools
from harnest.testing import _run_adk_evals


class _ToolModel(BaseLlm):
    """Drive ADK's actual tool dispatch without any external model calls."""

    tool_name: str

    async def generate_content_async(self, llm_request, stream=False):
        """Call one tool and return a deterministic final response."""
        complete = any(part.function_response is not None
                       for item in llm_request.contents for part in item.parts or [])
        part = types.Part(text="done") if complete else types.Part(
            function_call=types.FunctionCall(name=self.tool_name, args={})
        )
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


def _application(function, **kwargs):
    """Use the same managed native agent type exported by the compiler."""
    root = Agent(name="context_probe", instruction="Call the tool once.",
                 model=_ToolModel(model="offline", tool_name=function.__name__),
                 tools=[function]).build()
    return CompiledApplication(
        name=root.name, framework="adk", mode="managed", target=root,
        native_app=App(name=root.name, root_agent=root), **kwargs,
    )


def _eval_set(tool_name, *, expected=None, cases=1):
    """Use an exact non-LLM trajectory metric to distinguish execution and quality."""
    return EvalSet.model_validate({
        "eval_set_id": "context_probe",
        "eval_cases": [{"eval_id": f"case_{number}", "conversation": [{
            "invocation_id": "turn_1",
            "user_content": {"role": "user", "parts": [{"text": "probe"}]},
            "final_response": {"role": "model", "parts": [{"text": "done"}]},
            "intermediate_data": {"tool_uses": [{"name": expected or tool_name, "args": {}}]},
        }]} for number in range(cases)],
    })


async def _evaluate(application, eval_set, *, driver=None):
    """Exercise the same isolated native app used by playground evaluations."""
    async with adk_evaluation_runtime(application, driver):
        with adk_eval_agent_module(application) as module_name:
            await AgentEvaluator.evaluate_eval_set(
                agent_module=module_name, eval_set=eval_set, num_runs=1,
                eval_config=EvalConfig(criteria={"tool_trajectory_avg_score": 1.0}),
                print_detailed_results=False,
            )


def _listener(phase, callback, name):
    """Build compiler-shaped listeners without filesystem fixture overhead."""
    return LifecycleListener(phase=phase, callback=callback, order=0,
                             relative_path="extensions/probe.py", line=1,
                             function_name=name, context_name=name)


class ADKEvalContextTests(unittest.TestCase):
    def test_builtin_skill_discovery_runs_in_native_evaluator(self):
        """Regression: Harnest's actual built-in list_skills used to raise."""
        function = create_skill_tools(SkillScope())[0]
        application = _application(function)
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            asyncio.run(_evaluate(application, _eval_set(function.__name__)))
        self.assertIsNone(optional_active_context())
        self.assertEqual(application.native_app.plugins, [])

    def test_cli_resources_session_identity_and_cleanup(self):
        """CLI evaluation binds static/dynamic resources and revokes every case."""
        seen, lifecycle = [], []

        @contextmanager
        def client():
            lifecycle.append("open")
            try:
                yield "client"
            finally:
                lifecycle.append("close")

        def invocation_resource():
            return context.session_id

        @tool
        async def audit_session() -> str:
            """Read the identity used by session-aware application audit logging."""
            seen.append(context.current())
            self.assertEqual(context.resource("static"), "configured")
            self.assertEqual(context.resource("client"), "client")
            self.assertEqual(context.resource("request"), context.session_id)
            self.assertIsNone(await context.session.get("probe"))
            await context.session.set("probe", "stored")
            self.assertEqual(await context.session.get("probe"), "stored")
            return context.session_id

        application = _application(
            audit_session, context_values=(ContextValue("static", "configured", "test:static"),),
            extensions=(_listener("resource", client, "client"),
                        _listener("context", invocation_resource, "request")),
        )
        status, payload = self._cli(application, _eval_set("audit_session", cases=2))
        self.assertEqual(status, 0)
        self.assertEqual(payload["summary"]["passedCases"], 2)
        self.assertEqual(lifecycle, ["open", "close"])
        self.assertEqual(len({item.session_id for item in seen}), 2)
        for active in seen:
            with self.assertRaisesRegex(ContextUnavailableError, "invocation has finished"):
                active.resource("static")
        self.assertIsNone(optional_active_context())

    def test_playground_borrows_resources_without_closing_live_sessions(self):
        """Evaluation must not reopen resources or write to live session storage."""
        lifecycle = []
        sessions = InMemorySessionStore()

        @contextmanager
        def client():
            lifecycle.append("open")
            try:
                yield "client"
            finally:
                lifecycle.append("close")

        @tool
        async def audit_session() -> str:
            """Use application resources and isolated evaluation session data."""
            self.assertEqual(context.resource("client"), "client")
            await context.session.set("probe", True)
            return context.session_id

        application = _application(audit_session, session_store=sessions,
                                   extensions=(_listener("resource", client, "client"),))

        async def run():
            driver = _runtime_driver(application)
            try:
                await start_runtime_pipeline(driver)
                await _evaluate(application, _eval_set("audit_session"), driver=driver)
                self.assertEqual(lifecycle, ["open"])
                self.assertEqual(tuple(await sessions.list(user_id="test_user_id")), ())
            finally:
                await driver.close()

        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            asyncio.run(run())
        self.assertEqual(lifecycle, ["open", "close"])
        self.assertEqual(application.native_app.plugins, [])

    def test_unhandled_tool_error_is_not_a_scored_failure(self):
        """Native ADK's unscored assertion becomes a redacted execution error."""
        retained = []

        @tool
        def broken_tool() -> str:
            """Raise private provider text which must not enter the public message."""
            retained.append(context.current())
            raise RuntimeError("synthetic-api-key")

        application = _application(broken_tool)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(EvaluationExecutionError, "not evidence") as caught:
                self._cli(application, _eval_set("broken_tool"), directory=directory)
            payload = json.loads((Path(directory) / "result.json").read_text())
        self.assertNotIn("synthetic-api-key", str(caught.exception))
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error"]["type"], "EvaluationExecutionError")
        self.assertNotIn("synthetic-api-key", json.dumps(payload["error"]))
        for active in retained:
            with self.assertRaises(ContextUnavailableError):
                active._require_active()

    def test_genuine_quality_failure_remains_scored(self):
        """A completed tool trajectory mismatch remains an ordinary failed eval."""
        @tool
        def audit_session() -> str:
            """Return managed identity without changing the expected trajectory."""
            return context.session_id

        status, payload = self._cli(_application(audit_session),
                                    _eval_set("audit_session", expected="different_tool"))
        self.assertEqual(status, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertNotIn("error", payload)

    def test_recovered_run_does_not_relabel_scored_assertion(self):
        """Error observation alone does not mean ADK's final run was unscored."""
        from types import SimpleNamespace

        with self.assertRaisesRegex(AssertionError, "metric failed"):
            with evaluation_error_boundary(SimpleNamespace(message="earlier failure")):
                raise AssertionError("metric failed")

    def test_cli_closes_model_transport_exactly_once(self):
        """The full eval runtime, not both it and the CLI fallback, owns clients."""
        from harnest.model_lifecycle import _attach_lifecycle_resource

        closed = []

        class Client:
            async def aclose(self):
                """Count ownership calls even if real clients tolerate duplicates."""
                closed.append("close")

        @tool
        def audit_session() -> str:
            """Exercise actual managed inference before transport shutdown."""
            return context.session_id

        application = _application(audit_session)
        _attach_lifecycle_resource(application.target, Client())
        status, _ = self._cli(application, _eval_set("audit_session"))
        self.assertEqual(status, 0)
        self.assertEqual(closed, ["close"])

    def test_wrapped_callback_context_error_keeps_specific_guidance(self):
        """ADK wraps native plugin exceptions; only inspect types in the chain."""
        from harnest.eval_errors import execution_error_message

        cause = ContextUnavailableError("private-token")
        wrapped = RuntimeError("provider echoed private-token")
        wrapped.__cause__ = cause
        message = execution_error_message(wrapped)
        self.assertIn("invocation context", message)
        self.assertIn("list_skills", message)
        self.assertNotIn("private-token", message)

    def test_playground_exposes_safe_execution_error_details(self):
        """Return an actionable HTTP error instead of a blank generic 500."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from harnest.eval_errors import execution_error_message
        from harnest.playground import create_playground_router

        message = execution_error_message(ContextUnavailableError("private-token"))
        service = SimpleNamespace(run=AsyncMock(side_effect=EvaluationExecutionError(message)))
        app = FastAPI()
        app.include_router(create_playground_router(eval_service=service))
        with TestClient(app) as client:
            response = client.post("/_harnest/evals/run", json={"suiteId": "probe"})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], message)
        self.assertNotIn("private-token", response.text)

    def test_cli_evaluation_does_not_start_background_task_workers(self):
        """Evaluating an agent must not consume unrelated deployed queue rows."""
        from harnest.task import CompiledTask, TaskCallable, TaskDefinition

        def queued_work():
            """Represent a valid authored task that this eval does not request."""
            raise AssertionError("unrelated queued work must not execute")

        definition = TaskDefinition(queued_work, "default", 0)
        compiled = CompiledTask("queued_work", "tasks/queued_work.py", definition,
                                TaskCallable(definition))

        @tool
        def audit_session() -> str:
            """Require managed identity without dispatching queued work."""
            return context.session_id

        application = _application(audit_session, tasks=(compiled,))
        with patch("harnest.runtime_task.TaskRuntimeManager",
                   side_effect=AssertionError("evaluation must not start task workers")):
            status, _ = self._cli(application, _eval_set("audit_session"))
        self.assertEqual(status, 0)

    def _cli(self, application, eval_set, *, directory=None):
        """Run real CLI evaluation against an imported compiler-shaped module."""
        if directory is None:
            with tempfile.TemporaryDirectory() as directory:
                return self._cli(application, eval_set, directory=directory)
        artifact = Path(directory)
        module_name = f"{artifact.name}.agent"
        module = ModuleType(module_name)
        module.root_agent = application.target
        module.app = application.native_app
        module.application = application
        path = artifact / "probe.evalset.json"
        path.write_text(eval_set.model_dump_json(), encoding="utf-8")
        config = artifact / "test_config.json"
        config.write_text(json.dumps({"criteria": {"tool_trajectory_avg_score": 1.0}}))
        output = artifact / "result.json"
        with patch.dict(sys.modules, {module_name: module}), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            status = _run_adk_evals(artifact, EvalSuite((path,), config),
                                    print_results=False, result_output=output)
        return status, json.loads(output.read_text())
