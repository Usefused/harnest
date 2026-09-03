"""Evaluation entrypoints borrow transport only during the owning runtime scope."""

import asyncio
from contextlib import asynccontextmanager, contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from google.adk.evaluation.eval_config import EvalConfig
from google.adk.models import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from harnest.bundle import EvalSuite
from harnest.eval_model_transport import (
    close_owned_eval_model_transports,
    restore_eval_model_names,
)
from harnest.model_lifecycle import _attach_lifecycle_resource
from harnest.model_transport import (
    ModelTransportBinding,
    attach_model_transport_binding,
    propagate_model_transport_bindings,
)
from harnest.testing import _model_json, _run_adk_evals, _run_langgraph_evals


def _target():
    """Attach explicit gateway configuration without allocating an HTTP client."""

    return attach_model_transport_binding(
        SimpleNamespace(),
        model="openai/agent",
        completion_args={"api_base": "https://gateway.invalid/v1"},
    )


def _config():
    """Select a different judge on the agent's compatible gateway."""

    return EvalConfig.model_validate({
        "criteria": {"final_response_match_v2": {
            "threshold": 1.0,
            "judgeModelOptions": {"judgeModel": "openai/judge"},
        }},
    })


class _OfflineModel(BaseLlm):
    """Return valid deterministic agent and judge responses without networking."""

    async def generate_content_async(self, llm_request, stream=False):
        """Let ADK exercise its full inference and evaluation orchestration."""

        answer = "Paris." if self.model == "agent" else '{"is_the_agent_response_valid":"valid"}'
        yield LlmResponse(content=types.Content(
            role="model", parts=[types.Part(text=answer)]
        ))


class EvalTransportEntrypointTests(unittest.TestCase):
    def test_real_adk_evaluator_resolves_transport_and_persists_original_model(self):
        """Exercise ADK's service stack and collector, not just a mocked runner."""

        from google.adk.agents import LlmAgent
        from google.adk.apps import App

        root = LlmAgent(name="offline_agent", model=_OfflineModel(model="agent"))
        propagate_model_transport_bindings(_target(), root)
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory)
            module_name = f"{artifact.name}.agent"
            module = ModuleType(module_name)
            module.root_agent = root
            module.app = App(name="harnest_eval", root_agent=root)
            path = artifact / "offline.evalset.json"
            path.write_text(json.dumps({
                "eval_set_id": "offline",
                "eval_cases": [{"eval_id": "city", "conversation": [{
                    "invocation_id": "turn-1",
                    "user_content": {"role": "user", "parts": [{"text": "Which city?"}]},
                    "final_response": {"role": "model", "parts": [{"text": "Paris."}]},
                }]}],
            }), encoding="utf-8")
            output = artifact / "result.json"
            with patch.dict(sys.modules, {module_name: module}), patch(
                "harnest.testing._eval_config", side_effect=lambda *_: _config()
            ), patch.object(
                ModelTransportBinding, "build_eval_model",
                side_effect=lambda model: _OfflineModel(model=model),
            ) as build, redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                status = _run_adk_evals(
                    artifact, EvalSuite((path,), None),
                    print_results=False, result_output=output,
                )
            payload = output.read_text(encoding="utf-8")
        self.assertEqual(status, 0)
        build.assert_called_with("openai/judge")
        self.assertEqual(json.loads(payload)["summary"]["passedCases"], 1)
        self.assertIn("openai/judge", payload)
        self.assertNotIn("harnest_eval/", payload)

    def test_cleanup_failure_preserves_primary_cancellation_without_secret_text(self):
        """A failed close must not replace cancellation or disclose provider text."""

        target = _target()
        resource = SimpleNamespace(aclose=AsyncMock(
            side_effect=RuntimeError("synthetic-private-provider-error")
        ))
        _attach_lifecycle_resource(target, resource)
        primary = asyncio.CancelledError()
        asyncio.run(close_owned_eval_model_transports(target, primary_error=primary))
        resource.aclose.assert_awaited_once()
        self.assertNotIn("synthetic-private", str(getattr(primary, "__notes__", [])))

    def _assert_scoped_config(self, config):
        """Verify routing is private while serialized diagnostics stay authored."""

        criterion = config.criteria["final_response_match_v2"].model_dump(by_alias=True)
        model = criterion["judgeModelOptions"]["judgeModel"]
        self.assertTrue(model.startswith("harnest_eval/"))
        self.assertEqual(restore_eval_model_names(model), "openai/judge")
        payload = _model_json(config)
        self.assertNotIn("harnest_eval/", str(payload))
        self.assertIn("openai/judge", str(payload))

    def test_adk_cli_scopes_and_closes_on_success_and_error(self):
        """A CLI-owned agent closes once on the same loop after borrowers finish."""

        for failure in (None, RuntimeError("offline failure")):
            with self.subTest(failure=type(failure).__name__):
                self._run_adk_case(failure)

    def _run_adk_case(self, failure):
        """Run a real CLI wrapper with an isolated imported agent and evaluator."""

        target = _target()
        events = []

        async def evaluate(*_args, config, **_kwargs):
            """Emulate ADK consuming the rewritten config inside the live scope."""

            self._assert_scoped_config(config)
            events.append("evaluate")
            if failure is not None:
                raise failure

        async def close(received, *, primary_error):
            """Observe ownership cleanup after transport access is revoked."""

            self.assertIs(received, target)
            self.assertIs(primary_error, failure)
            events.append("close")

        @contextmanager
        def imported(module_name):
            """Make the loaded root discoverable without compiling an artifact."""

            module = ModuleType(module_name)
            module.root_agent = target
            with patch.dict(sys.modules, {module_name: module}):
                yield

        with tempfile.TemporaryDirectory() as directory, patch(
            "harnest.testing._eval_dependencies", return_value=(object(), object())
        ), patch("harnest.testing._eval_config", side_effect=lambda *_: _config()), patch(
            "harnest.testing._adk_eval_output_filter", imported
        ), patch("harnest.testing._evaluate_eval_sets", evaluate), patch(
            "harnest.testing.close_owned_eval_model_transports", close
        ), redirect_stderr(StringIO()):
            if failure is None:
                self.assertEqual(_run_adk_evals(
                    Path(directory), EvalSuite((), None), print_results=False
                ), 0)
            else:
                with self.assertRaisesRegex(RuntimeError, "offline failure"):
                    _run_adk_evals(
                        Path(directory), EvalSuite((), None), print_results=False
                    )
        self.assertEqual(events, ["evaluate", "close"])

    def test_langgraph_scopes_before_driver_cleanup(self):
        """The graph runtime keeps ownership and outlives every eval borrower."""

        application = SimpleNamespace(target=_target())
        events = []

        @asynccontextmanager
        async def driver(_application):
            """Expose an adapter while retaining cleanup in its owning driver."""

            events.append("start")
            try:
                yield "test_graph_eval"
            finally:
                events.append("close")

        async def evaluate(*_args, config, **_kwargs):
            """Inspect the configuration at the actual evaluator boundary."""

            self._assert_scoped_config(config)
            events.append("evaluate")

        with patch(
            "harnest.testing._eval_dependencies", return_value=(object(), object())
        ), patch("harnest.testing._eval_config", side_effect=lambda *_: _config()), patch(
            "harnest.eval_langgraph.langgraph_eval_agent_module", driver
        ), patch("harnest.testing._evaluate_eval_sets", evaluate), patch(
            "harnest.testing.close_owned_eval_model_transports", new_callable=AsyncMock
        ) as cli_close:
            self.assertEqual(_run_langgraph_evals(
                application, EvalSuite((), None), print_results=False
            ), 0)
        cli_close.assert_not_awaited()
        self.assertEqual(events, ["start", "evaluate", "close"])
