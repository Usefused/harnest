"""Judge parsers must see final verdicts, never provider reasoning parts."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from google.adk.evaluation.eval_case import Invocation
from google.adk.evaluation.eval_config import EvalConfig, get_eval_metrics_from_config
from google.adk.evaluation.rubric_based_final_response_quality_v1 import RubricBasedFinalResponseQualityV1Evaluator
from google.adk.models import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.models.registry import LLMRegistry
from google.genai import types
from pydantic import PrivateAttr

from harnest.eval_model_transport import _judge_final_response, eval_model_transports
from harnest.model_transport import ModelTransportBinding
from test_eval_model_transport import _config, _judge, _responses, _target


def _response(*parts, **options):
    """Construct native ADK responses with independent metadata and content parts."""

    return LlmResponse(content=types.Content(role="model", parts=list(parts)), **options)


class _SequenceModel(BaseLlm):
    _responses = PrivateAttr()

    def __init__(self, model, responses):
        """Keep controlled provider responses outside Pydantic's public fields."""

        super().__init__(model=model)
        self._responses = responses

    async def generate_content_async(self, llm_request, stream=False):
        """Exercise the real judge loop with non-streaming provider event sequences."""

        for response in self._responses:
            yield response


class JudgeOutputTests(unittest.IsolatedAsyncioTestCase):
    def test_mixed_response_is_copied_without_losing_metadata_or_final_rationale(self):
        """Filter provider labels, not prose that happens to discuss reasoning."""

        visible = types.Part(text="Rationale: reasoning is relevant.\nVerdict: yes")
        response = _response(types.Part(text="Verdict: no", thought=True), visible,
                             turn_complete=True, usage_metadata=types.GenerateContentResponseUsageMetadata(total_token_count=17))
        original = response.model_dump()
        result = _judge_final_response(response)
        self.assertEqual(result.content.parts, [visible])
        self.assertEqual(result.usage_metadata.total_token_count, 17)
        self.assertTrue(result.turn_complete)
        self.assertEqual(response.model_dump(), original)
        self.assertIsNot(result.content, response.content)

    def test_empty_and_error_responses_are_not_fabricated_verdicts(self):
        """No answer stays unscorable; provider errors retain their original metadata."""

        for response in (LlmResponse(), LlmResponse(error_code="provider_error", error_message="synthetic")):
            self.assertIs(_judge_final_response(response), response)
        self.assertIsNone(_judge_final_response(_response(types.Part(text="Verdict: yes", thought=True))))
        self.assertIsNone(_judge_final_response(_response(types.Part(text="Verdict:"), partial=True)))

    async def test_same_model_judge_filters_thoughts_but_simulator_is_unchanged(self):
        """Role-specific aliases must not alter other callers of the same model."""

        response = _response(types.Part(text="private", thought=True), types.Part(text="final"))
        delegate = _SequenceModel("openai/shared", [response])
        with patch.object(ModelTransportBinding, "build_eval_model", return_value=delegate):
            with eval_model_transports(_target(), _config("openai/shared", "openai/shared")) as prepared:
                self.assertNotEqual(_judge(prepared), prepared.user_simulator_config.model)
                judged = await _responses(LLMRegistry.new_llm(_judge(prepared)))
                simulated = await _responses(LLMRegistry.new_llm(prepared.user_simulator_config.model))
        self.assertEqual([p.text for p in judged[0].content.parts], ["final"])
        self.assertIs(simulated[0], response)
        self.assertTrue(response.content.parts[0].thought)

    async def test_native_unbound_judge_keeps_its_provider_factory(self):
        """Native judges need output filtering even without Harnest transport metadata."""

        response = _response(types.Part(text="private", thought=True), types.Part(text="final"))
        original = LLMRegistry.resolve
        requested = []

        def resolve(model):
            """Intercept only the authored provider, not Harnest's real registry proxy."""

            if model == "native-judge":
                requested.append(model)
                return lambda model: _SequenceModel(model, [response])
            return original(model)

        with patch.object(LLMRegistry, "resolve", side_effect=resolve):
            with eval_model_transports(SimpleNamespace(), _config("native-judge")) as prepared:
                judged = await _responses(LLMRegistry.new_llm(_judge(prepared)))
        self.assertEqual(requested, ["native-judge"])
        self.assertEqual([p.text for p in judged[0].content.parts], ["final"])

    async def test_real_rubric_parser_uses_final_verdict_after_thought_and_partial_events(self):
        """Run ADK's evaluator and rubric parser, not a substitute text parser."""

        final = "Property: The answer is correct.\nRationale: It matches.\nVerdict: yes"
        draft = "Property: The answer is correct.\nRationale: Draft only.\nVerdict: no"
        responses = [
            _response(types.Part(text=draft, thought=True)),
            _response(types.Part(text=draft), partial=True),
            _response(types.Part(text=draft, thought=True), types.Part(text=final)),
        ]
        config = EvalConfig.model_validate({"criteria": {"rubric_based_final_response_quality_v1": {
            "threshold": 1.0,
            "rubrics": [{"rubric_id": "correct", "rubric_content": {"text_property": "The answer is correct."}}],
            "judgeModelOptions": {"judgeModel": "openai/judge", "numSamples": 1},
        }}})
        invocation = Invocation(user_content=types.Content(parts=[types.Part(text="What is 2+2?")]),
                                final_response=types.Content(parts=[types.Part(text="4")]))
        delegate = _SequenceModel("openai/judge", responses)
        with patch.object(ModelTransportBinding, "build_eval_model", return_value=delegate):
            with eval_model_transports(_target(), config) as prepared:
                evaluator = RubricBasedFinalResponseQualityV1Evaluator(get_eval_metrics_from_config(prepared)[0])
                result = await evaluator.evaluate_invocations([invocation])
        self.assertEqual(result.overall_score, 1.0)
        scores = result.per_invocation_results[0].rubric_scores
        self.assertEqual([(score.rubric_id, score.score) for score in scores], [("correct", 1.0)])
