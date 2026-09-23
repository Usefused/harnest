"""Conversion contracts for optional Harnest custom metric helpers."""

import asyncio
import math
import unittest

from google.adk.evaluation.eval_case import Invocation
from google.adk.evaluation.eval_metrics import BaseCriterion, EvalMetric, EvalStatus
from google.adk.evaluation.evaluator import EvaluationResult
from google.genai import types

from harnest.evaluation import MetricContext, MetricScore, metric


def invocation(text):
    """Build real ADK invocation evidence without executing a model."""
    return Invocation(
        user_content=types.Content(role="user", parts=[types.Part(text=text)])
    )


class MetricAdapterTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the adapter using the same positional contract as ADK's registry."""

    def setUp(self):
        """Create independent native metric and multi-turn evidence for each test."""
        self.metric = EvalMetric(
            metric_name="company", criterion=BaseCriterion(threshold=0.8)
        )
        self.actual = [invocation("first"), invocation("second")]

    async def test_conversation_score_is_counted_once_and_retains_evidence(self):
        """Whole-case scoring must agree with ADK's scored-turn average."""

        @metric
        def score(context):
            self.assertIsInstance(context, MetricContext)
            self.assertIs(context.metric, self.metric)
            self.assertIs(context.actual[0], self.actual[0])
            self.assertEqual(context.threshold, 0.8)
            self.assertIsNone(context.expected)
            return MetricScore(0.8, "The service verified both answers.")

        result = await score(self.metric, self.actual, None, None)
        self.assertEqual(result.overall_score, 0.8)
        self.assertEqual(result.overall_eval_status, EvalStatus.PASSED)
        self.assertEqual([r.score for r in result.per_invocation_results], [None, 0.8])
        self.assertEqual(
            result.per_invocation_results[0].eval_status, EvalStatus.NOT_EVALUATED
        )
        self.assertEqual(
            result.overall_rubric_scores[0].rationale,
            "The service verified both answers.",
        )
        self.assertEqual(
            result.per_invocation_results[-1].rubric_scores,
            result.overall_rubric_scores,
        )

    async def test_async_turn_scores_use_mean_and_handle_short_expected_history(self):
        """Await service scorers and never truncate actual turns to golden length."""

        @metric
        async def score(context):
            await asyncio.sleep(0)
            self.assertEqual(context.scenario, "scenario-sentinel")
            return [MetricScore(0.5, "Missing fact"), MetricScore(0.9, "Correct")]

        result = await score(
            self.metric, self.actual, self.actual[:1], "scenario-sentinel"
        )
        self.assertAlmostEqual(result.overall_score, 0.7)
        self.assertEqual(result.overall_eval_status, EvalStatus.FAILED)
        self.assertEqual(
            [r.eval_status for r in result.per_invocation_results],
            [EvalStatus.FAILED, EvalStatus.PASSED],
        )
        self.assertIsNone(result.per_invocation_results[1].expected_invocation)
        self.assertEqual(
            result.per_invocation_results[0].expected_invocation, self.actual[0]
        )

    async def test_unscored_and_empty_cases_never_synthesize_a_pass(self):
        """Missing evidence must remain explicit in the normal evaluator contract."""
        for scores, actual in (
            ([MetricScore(None), MetricScore(None)], self.actual),
            ([], []),
        ):
            with self.subTest(scores=scores):

                @metric
                def score(context):
                    return scores

                result = await score(self.metric, actual)
                self.assertIsNone(result.overall_score)
                self.assertEqual(result.overall_eval_status, EvalStatus.NOT_EVALUATED)

    async def test_mean_ignores_only_explicitly_unscored_turns(self):
        """Mixed scored and unscored results use the evaluator's aggregate policy."""

        @metric
        def score(context):
            return [MetricScore(None), MetricScore(0.9)]

        result = await score(self.metric, self.actual)
        self.assertEqual(result.overall_score, 0.9)
        self.assertEqual(result.overall_eval_status, EvalStatus.PASSED)

    async def test_native_result_passes_through_without_rewriting(self):
        """Advanced callers retain ownership of the native result contract."""
        native = EvaluationResult()

        @metric
        def score(context):
            return native

        self.assertIs(await score(self.metric, self.actual), native)

    async def test_invalid_result_shapes_fail_instead_of_dropping_turns(self):
        """A malformed hosted response must not become an accidental passing score."""
        for value in (0.9, "0.9", [MetricScore(1)], [1, 1], {"score": 1}):
            with self.subTest(value=value):

                @metric
                def score(context):
                    return value

                with self.assertRaises((ValueError, TypeError)):
                    await score(self.metric, self.actual)

        @metric
        def conversation(context):
            return MetricScore(1)

        with self.assertRaisesRegex(ValueError, "at least one actual"):
            await conversation(self.metric, [])

    async def test_service_errors_propagate_and_cancellation_is_not_swallowed(self):
        """Leave infrastructure failure and cancellation handling to the shared runner."""
        for error in (RuntimeError("service unavailable"), asyncio.CancelledError()):
            with self.subTest(error=type(error)):

                @metric
                async def score(context):
                    raise error

                with self.assertRaises(type(error)):
                    await score(self.metric, self.actual)


class MetricScoreTests(unittest.TestCase):
    """Validate the convenience result without importing ADK into authored scoring logic."""

    def test_invalid_numbers_and_evidence_are_rejected(self):
        """NaN, infinity and boolean values must not corrupt a quality gate."""
        for value in (True, "1", math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                MetricScore(value)
        with self.assertRaises(TypeError):
            MetricScore(1, {"reason": "not text"})
        with self.assertRaises(TypeError):
            metric(None)

    def test_custom_score_ranges_are_preserved(self):
        """The authored metricInfo range, not the adapter, defines score scale."""
        self.assertEqual(MetricScore(85, "85 out of 100").score, 85)
