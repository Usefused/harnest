"""Offline isolation and ownership checks for borrowed evaluation transports."""

import asyncio
from contextlib import contextmanager, nullcontext
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from google.adk.evaluation.eval_config import EvalConfig
from google.adk.evaluation.eval_set import EvalSet
from google.adk.models import BaseLlm
from google.adk.models._capabilities import LlmCapabilities
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.models.registry import LLMRegistry
from google.genai import types
from pydantic import PrivateAttr

from harnest.eval_model_transport import (
    close_owned_eval_model_transports,
    eval_model_transports,
    restore_eval_model_names,
)
from harnest.bundle import EvalSuite
from harnest.evaluation import EvaluationError
from harnest.model_lifecycle import _attach_lifecycle_resource
from harnest.model_transport import (
    ModelTransportBinding,
    attach_model_transport_binding,
    model_transport_bindings,
    propagate_model_transport_bindings,
)
from harnest.playground_eval import PlaygroundEvalService


class _RecordingModel(BaseLlm):
    """Exercise the actual ADK registry without contacting any provider."""

    _recorder = PrivateAttr()

    def __init__(self, model, recorder):
        """Retain recording state outside the model's validated public fields."""

        super().__init__(model=model)
        self._recorder = recorder

    @property
    def capabilities(self):
        """Expose a non-default capability to detect accidental alias lookup."""

        return LlmCapabilities(output_schema_and_tools=True)

    async def generate_content_async(self, llm_request, stream=False):
        """Yield a transport-specific response after allowing competing tasks in."""

        self._recorder.calls.append((llm_request, stream))
        await asyncio.sleep(0)
        yield LlmResponse(content=types.Content(
            role="model", parts=[types.Part(text=self._recorder.label)]
        ))


def _target(model="openai/source", label="transport"):
    """Attach a real binding while keeping all test credentials synthetic."""

    target = SimpleNamespace(sub_agents=[])
    attach_model_transport_binding(
        target, model=model,
        completion_args={"api_base": f"https://{label}.invalid/v1"},
    )
    return target


def _config(judge="openai/judge", simulator="openai/simulator"):
    """Use ADK's public config schema for both judge and multi-turn models."""

    return EvalConfig.model_validate({
        "criteria": {
            "final_response_match_v2": {
                "threshold": 1,
                "judgeModelOptions": {"judgeModel": judge},
            },
            "tool_trajectory_avg_score": 1,
        },
        "userSimulatorConfig": {"type": "llm_backed", "model": simulator},
    })


def _judge(config):
    """Read the preserved criterion extras using ADK's serialized aliases."""

    criterion = config.model_dump(by_alias=True)["criteria"]["final_response_match_v2"]
    return criterion["judgeModelOptions"]["judgeModel"]


@contextmanager
def _recording_transports(*targets):
    """Replace only adapter construction, leaving discovery and registry real."""

    records = {
        id(model_transport_bindings(target)[0]): SimpleNamespace(
            label=f"transport-{index}", calls=[], models=[]
        )
        for index, target in enumerate(targets)
    }

    def build(binding, model_name):
        """Record which binding the public proxy selected for the explicit ID."""

        recorder = records[id(binding)]
        recorder.models.append(model_name)
        return _RecordingModel(model=model_name, recorder=recorder)

    with patch.object(ModelTransportBinding, "build_eval_model", new=build):
        yield list(records.values())


async def _responses(model, request=None):
    """Consume the async model boundary without invoking any evaluator network I/O."""

    request = request or LlmRequest(model=model.model)
    return [response async for response in model.generate_content_async(request)]


class EvalModelTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_config_is_copied_and_explicit_models_are_restored(self):
        """Judge and simulator share transport without changing authored settings."""

        target = _target()
        config = _config()
        original = config.model_dump(by_alias=True)
        with _recording_transports(target) as records:
            with eval_model_transports(target, config) as prepared:
                judge_alias = _judge(prepared)
                simulator_alias = prepared.user_simulator_config.model
                self.assertIsNot(prepared, config)
                self.assertNotEqual(judge_alias, simulator_alias)
                self.assertTrue(judge_alias.startswith("harnest_eval/"))
                judge = LLMRegistry.new_llm(judge_alias)
                simulator = LLMRegistry.new_llm(simulator_alias)
                request = LlmRequest(model=judge_alias)
                await _responses(judge, request)
                await _responses(simulator)
                self.assertEqual(judge.model, "openai/judge")
                self.assertTrue(judge.capabilities.output_schema_and_tools)
                self.assertEqual(records[0].models, ["openai/judge", "openai/simulator"])
                self.assertEqual(records[0].calls[0][0].model, "openai/judge")
                self.assertIsNot(records[0].calls[0][0], request)
                self.assertEqual(request.model, judge_alias)
                self.assertEqual(
                    restore_eval_model_names(prepared.model_dump(by_alias=True)), original
                )
                self.assertEqual(restore_eval_model_names({
                    "models": [judge_alias, {"simulator": simulator_alias}],
                    "text": f"prefix {judge_alias}",
                }), {
                    "models": ["openai/judge", {"simulator": "openai/simulator"}],
                    "text": f"prefix {judge_alias}",
                })
        self.assertEqual(config.model_dump(by_alias=True), original)

    async def test_concurrent_runs_keep_transport_authority_separate(self):
        """Overlapping tasks must never select the other application's gateway."""

        first, second = _target(label="first"), _target(label="second")
        entered = [asyncio.Event(), asyncio.Event()]

        async def run(target, index):
            """Keep both scopes live before resolving either identical model ID."""

            with eval_model_transports(target, _config()) as prepared:
                alias = _judge(prepared)
                entered[index].set()
                await entered[1 - index].wait()
                result = await _responses(LLMRegistry.new_llm(alias))
                return alias, result[0].content.parts[0].text

        with _recording_transports(first, second):
            results = await asyncio.gather(run(first, 0), run(second, 1))
        self.assertNotEqual(results[0][0], results[1][0])
        self.assertEqual([result[1] for result in results], ["transport-0", "transport-1"])

    async def test_expired_proxy_cannot_call_borrowed_transport(self):
        """Leaving an eval revokes adapters retained by a caller or background task."""

        target = _target()
        with _recording_transports(target) as records:
            with eval_model_transports(target, _config()) as prepared:
                proxy = LLMRegistry.new_llm(_judge(prepared))
            with self.assertRaisesRegex(EvaluationError, "no longer active"):
                await _responses(proxy)
        self.assertEqual(records[0].calls, [])

    async def test_revocation_rejects_in_flight_response_in_inherited_context(self):
        """A background task's copied ContextVar cannot outlive its eval authority."""

        target = _target()
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed_response(model, llm_request, stream=False):
            """Hold a provider response until its owning eval scope has exited."""

            started.set()
            await release.wait()
            yield LlmResponse(content=types.Content(
                role="model", parts=[types.Part(text="late response")]
            ))

        with (
            _recording_transports(target),
            patch.object(_RecordingModel, "generate_content_async", new=delayed_response),
        ):
            with eval_model_transports(target, _config()) as prepared:
                proxy = LLMRegistry.new_llm(_judge(prepared))
                pending = asyncio.create_task(_responses(proxy))
                await started.wait()
            release.set()
            with self.assertRaisesRegex(EvaluationError, "no longer active"):
                await pending

    async def test_outer_proxy_is_rejected_inside_another_scope(self):
        """Nested evals cannot borrow each other's active authenticated adapters."""

        first, second = _target(label="first"), _target(label="second")
        with _recording_transports(first, second) as records:
            with eval_model_transports(first, _config()) as outer:
                alias = _judge(outer)
                proxy = LLMRegistry.new_llm(alias)
                with eval_model_transports(second, _config()):
                    with self.assertRaisesRegex(EvaluationError, "no longer active"):
                        await _responses(proxy)
                    with self.assertRaisesRegex(EvaluationError, "another run"):
                        LLMRegistry.new_llm(alias)
                await _responses(proxy)
        self.assertEqual(len(records[0].calls), 1)
        self.assertEqual(records[1].calls, [])

    def test_ambiguous_same_provider_fails_without_mutating_config(self):
        """Multiple gateways require an unambiguous authored model selection."""

        root = SimpleNamespace(sub_agents=[_target("openai/one"), _target("openai/two")])
        config = _config()
        original = config.model_dump(by_alias=True)
        with self.assertRaisesRegex(EvaluationError, "ambiguous agent model transport"):
            with eval_model_transports(root, config):
                self.fail("ambiguous transport unexpectedly authorized")
        self.assertEqual(config.model_dump(by_alias=True), original)

    async def test_exact_model_wins_over_other_compatible_transports(self):
        """An explicit model match takes precedence over provider fallback."""

        first, second = _target("openai/one"), _target("openai/two")
        root = SimpleNamespace(sub_agents=[first, second])
        with _recording_transports(first, second) as records:
            with eval_model_transports(root, _config("openai/one", "openai/two")) as prepared:
                await _responses(LLMRegistry.new_llm(_judge(prepared)))
                await _responses(LLMRegistry.new_llm(prepared.user_simulator_config.model))
        self.assertEqual(records[0].models, ["openai/one"])
        self.assertEqual(records[1].models, ["openai/two"])

    def test_unrelated_provider_and_numeric_criteria_are_unchanged(self):
        """Native providers must keep ADK's normal resolution and credential path."""

        config = _config("gemini-2.5-flash", "anthropic/claude-test")
        original = config.model_dump(by_alias=True)
        with eval_model_transports(_target(), config) as prepared:
            self.assertEqual(prepared.model_dump(by_alias=True), original)
        self.assertEqual(config.model_dump(by_alias=True), original)

    async def test_propagated_binding_and_cyclic_owners_are_deduplicated(self):
        """Wrapper metadata and an agent's model are references, not new gateways."""

        child = _target()
        root = SimpleNamespace(root_agent=SimpleNamespace(model=child), sub_agents=[child])
        child.sub_agents.append(root)
        propagate_model_transport_bindings(child, root)
        with _recording_transports(child) as records:
            with eval_model_transports(root, _config()) as prepared:
                await _responses(LLMRegistry.new_llm(_judge(prepared)))
        self.assertEqual(len(records[0].calls), 1)

    def test_target_without_explicit_transport_preserves_config(self):
        """Ordinary environment-only agents need no scoped ADK adapter."""

        config = _config()
        with eval_model_transports(SimpleNamespace(), config) as prepared:
            self.assertIs(prepared, config)

    async def test_borrowing_does_not_close_owner_and_cleanup_deduplicates(self):
        """Only the explicit CLI ownership boundary closes shared resources."""

        closed = []

        class Resource:
            async def aclose(self):
                """Record each cleanup attempt without allocating a real client."""

                closed.append(self)

        child = _target()
        root = SimpleNamespace(root_agent=child)
        resource = Resource()
        _attach_lifecycle_resource(child, resource)
        _attach_lifecycle_resource(root, resource)
        with eval_model_transports(root, _config()):
            self.assertEqual(closed, [])
        self.assertEqual(closed, [])
        await close_owned_eval_model_transports(root)
        self.assertEqual(closed, [resource])

    async def test_playground_scopes_transport_and_preserves_live_owner(self):
        """A playground run borrows its live server on success and every failure path."""

        for error in (None, AssertionError("metric failed"), RuntimeError("provider failed")):
            with self.subTest(error=type(error).__name__):
                await self._playground_case(error)

    async def _playground_case(self, error):
        """Exercise the service entrypoint while replacing only evaluator execution."""

        target = _target()
        resource = SimpleNamespace(aclose=AsyncMock())
        _attach_lifecycle_resource(target, resource)
        proxies = []

        async def evaluate(**kwargs):
            """Require an authorized model alias throughout the actual service call."""

            alias = _judge(kwargs["eval_config"])
            self.assertTrue(alias.startswith("harnest_eval/"))
            proxy = LLMRegistry.new_llm(alias)
            proxies.append(proxy)
            await _responses(proxy)
            resource.aclose.assert_not_called()
            if error is not None:
                # Both ordinary metric failure and exceptional provider failure
                # must unwind the borrowing scope without stopping the server.
                raise error

        evaluator = SimpleNamespace(evaluate_eval_set=evaluate)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.evalset.json"
            path.write_text('{"eval_set_id":"sample","eval_cases":[]}', encoding="utf-8")
            suite = EvalSuite((path,), None)
            with patch("harnest.playground_eval.discover_evals", return_value=suite):
                service = PlaygroundEvalService(
                    directory, SimpleNamespace(target=target, framework="adk"), None
                )
            with (
                patch("harnest.playground_eval.eval_config", return_value=_config()),
                patch("harnest.playground_eval.eval_dependencies", return_value=(evaluator, EvalSet)),
                patch.object(service, "_agent_module", return_value=nullcontext("test_agent")),
                _recording_transports(target) as records,
            ):
                if isinstance(error, RuntimeError):
                    with self.assertRaisesRegex(RuntimeError, "provider failed"):
                        await service.run("sample", "business")
                else:
                    result = await service.run("sample", "business")
                    self.assertEqual(result["status"], "failed" if error else "passed")
                self.assertEqual(len(records[0].calls), 1)
                with self.assertRaisesRegex(EvaluationError, "no longer active"):
                    await _responses(proxies[0])
        resource.aclose.assert_not_called()


if __name__ == "__main__":
    unittest.main()
