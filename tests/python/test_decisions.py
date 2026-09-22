"""Decision contracts, policy boundaries and isolated provider execution."""

import asyncio
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch

from harnest import context
from harnest.agent import tool
from harnest.context import activate_context, create_agent_context, revoke_context
from harnest.decisions import (
    Choice, ChoicePolicy, ChoiceResult, DecisionAction, DecisionBinding,
    DecisionCapabilities, DecisionDefinition, DecisionOutcome, DecisionProviderError,
    DecisionRequest, DecisionResponse, Decisions, DecisionTimeoutError,
    DecisionValidationError, FixtureDecisionProvider, Predicate, PredicateResult,
    QuestionKind, Score, ScoreResult, ThresholdPolicy,
)


CAPABILITIES = DecisionCapabilities(frozenset(QuestionKind), batching=True)
DEFINITION = DecisionDefinition("route", "1", (Choice("team", "Choose a team", {"a": "A", "b": "B"}),))
RESPONSE = DecisionResponse({"team": ChoiceResult("a")})


@tool
async def choose_team() -> str:
    """Exercise a decision through the same public surface as an authored tool."""
    result = await context.decisions.evaluate("route", {})
    return result.response.answers["team"].value


class Provider:
    capabilities = CAPABILITIES
    version = "1"

    def __init__(self, response=RESPONSE, error=None):
        self.response = response
        self.error = error
        self.calls = []

    async def evaluate(self, request):
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return self.response


def registry(provider=None, *, definition=DEFINITION, policy=None, on_error=None, timeout=1):
    """Build a single explicit binding without framework or provider dependencies."""
    return Decisions(
        providers={"custom": provider or Provider()},
        bindings=(DecisionBinding(definition, "custom", policy, on_error),),
        timeout_seconds=timeout,
    )


class DecisionContractTests(unittest.TestCase):
    def test_state_and_definitions_are_deeply_detached(self):
        state = {"ticket": {"parts": ["private"]}}
        request = DecisionRequest(DEFINITION, state)
        state["ticket"]["parts"].append("changed")
        self.assertEqual(request.state["ticket"]["parts"], ("private",))
        with self.assertRaises(TypeError):
            request.state["ticket"]["changed"] = True
        with self.assertRaises(FrozenInstanceError):
            DEFINITION.version = "2"
        self.assertNotIn("private", repr(request))

    def test_rejects_non_json_and_cyclic_state(self):
        cycle = {}
        cycle["self"] = cycle
        for state in ({"value": object()}, {1: "value"}, {"value": float("nan")}, cycle):
            with self.subTest(state_type=type(state)), self.assertRaises((TypeError, ValueError)):
                DecisionRequest(DEFINITION, state)

    def test_rejects_invalid_numbers_and_distributions(self):
        for value in (True, float("nan"), float("inf"), -0.1, 1.1):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                PredicateResult(value)
        with self.assertRaises(ValueError):
            ChoiceResult("a", {"a": 0.4, "b": 0.4})
        with self.assertRaises(ValueError):
            ScoreResult(1, (0.5, -0.1, 0.6))

    def test_registration_rejects_missing_provider_and_duplicate_decisions(self):
        binding = DecisionBinding(DEFINITION, "missing")
        with self.assertRaises(ValueError):
            Decisions(providers={}, bindings=(binding,))
        with self.assertRaises(ValueError):
            Decisions(providers={"missing": Provider()}, bindings=(binding, binding))

    def test_capability_and_policy_validation_happens_before_calls(self):
        provider = Provider()
        provider.capabilities = DecisionCapabilities(frozenset({QuestionKind.PREDICATE}))
        with self.assertRaises(ValueError):
            registry(provider)
        provider.capabilities = CAPABILITIES
        policy = ChoicePolicy("team", {"a": DecisionOutcome(DecisionAction.PROCEED)})
        with self.assertRaises(ValueError):
            registry(provider, policy=policy)
        self.assertEqual(provider.calls, [])

    def test_batched_questions_require_explicit_support(self):
        definition = DecisionDefinition("batch", "1", (Predicate("a", "A?"), Predicate("b", "B?")))
        provider = Provider()
        provider.capabilities = DecisionCapabilities(frozenset({QuestionKind.PREDICATE}))
        with self.assertRaises(ValueError):
            registry(provider, definition=definition)

    def test_failure_fallback_cannot_authorize_or_route(self):
        outcomes = (DecisionOutcome(DecisionAction.PROCEED), DecisionOutcome(DecisionAction.ROUTE, "agent"))
        for outcome in outcomes:
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                DecisionBinding(DEFINITION, "custom", on_error=outcome)

    def test_confidence_threshold_requires_native_confidence(self):
        policy = ChoicePolicy("team", {
            "a": DecisionOutcome(DecisionAction.ROUTE, "billing"),
            "b": DecisionOutcome(DecisionAction.ROUTE, "support"),
        }, minimum_confidence=0.8)
        with self.assertRaises(ValueError):
            registry(policy=policy)

    def test_question_and_policy_configuration_is_unambiguous(self):
        with self.assertRaises(ValueError):
            DecisionDefinition("duplicate", "1", (Predicate("q", "A?"), Predicate("q", "B?")))
        with self.assertRaises(ValueError):
            ThresholdPolicy("q", 0.5, 0.5)
        with self.assertRaises(ValueError):
            registry(definition=DecisionDefinition("gate", "1", (Predicate("q", "A?"),)),
                     policy=ThresholdPolicy("q", 0.2, 2))


class DecisionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_choice_routing_and_uncertainty(self):
        provider = Provider(DecisionResponse({"team": ChoiceResult("a", confidence=0.79)}))
        provider.capabilities = DecisionCapabilities(frozenset({QuestionKind.CHOICE}), confidence=True)
        policy = ChoicePolicy("team", {
            "a": DecisionOutcome(DecisionAction.ROUTE, "billing"),
            "b": DecisionOutcome(DecisionAction.BLOCK),
        }, minimum_confidence=0.8, uncertain=DecisionOutcome(DecisionAction.REVIEW))
        decisions = registry(provider, policy=policy)
        uncertain = await decisions.evaluate("route", {})
        self.assertEqual(uncertain.outcome.action, DecisionAction.REVIEW)
        provider.response = DecisionResponse({"team": ChoiceResult("a", confidence=0.8)})
        routed = await decisions.evaluate("route", {})
        self.assertEqual(routed.outcome.route, "billing")
        self.assertEqual(routed.provider_version, "1")

    async def test_thresholds_preserve_probability_and_rubric_scales(self):
        questions = (Predicate("q", "Grounded?"), Score("q", "Quality?", ("poor", "fair", "good")))
        for question in questions:
            definition = DecisionDefinition("gate", "1", (question,))
            for value, action in ((0.2, DecisionAction.BLOCK), (0.5, DecisionAction.ABSTAIN), (0.8, DecisionAction.PROCEED)):
                result = PredicateResult(value) if isinstance(question, Predicate) else ScoreResult(value)
                decisions = registry(Provider(DecisionResponse({"q": result})), definition=definition,
                                     policy=ThresholdPolicy("q", 0.2, 0.8))
                with self.subTest(question=type(question), value=value):
                    self.assertEqual((await decisions.evaluate("gate", {})).outcome.action, action)

    async def test_invalid_responses_cannot_reach_policy(self):
        invalid = (None, DecisionResponse({}), DecisionResponse({"team": ChoiceResult("unknown")}),
                   DecisionResponse({"team": PredicateResult(0.8)}),
                   DecisionResponse({"team": ChoiceResult("a", confidence=0.9)}))
        for response in invalid:
            provider = Provider(response)
            with self.subTest(response=response), self.assertRaises(DecisionValidationError):
                await registry(provider).evaluate("route", {})
            self.assertEqual(len(provider.calls), 1)

    async def test_distribution_and_score_coverage_is_validated(self):
        provider = Provider(DecisionResponse({"team": ChoiceResult("a", {"a": 1})}))
        provider.capabilities = DecisionCapabilities(frozenset({QuestionKind.CHOICE}), probabilities=True)
        with self.assertRaises(DecisionValidationError):
            await registry(provider).evaluate("route", {})
        definition = DecisionDefinition("score", "1", (Score("q", "Quality?", ("bad", "good")),))
        provider = Provider(DecisionResponse({"q": ScoreResult(2)}))
        with self.assertRaises(DecisionValidationError):
            await registry(provider, definition=definition).evaluate("score", {})

    async def test_native_metadata_is_preserved_and_required_when_advertised(self):
        definition = DecisionDefinition("scoring", "1", (
            Score("q", "Quality?", ("poor", "good")), Predicate("ready", "Ready?"),
        ))
        answer = ScoreResult(0.6, (0.25, 0.75), confidence=0.5)
        provider = Provider(DecisionResponse({"q": answer, "ready": PredicateResult(0.9)}))
        provider.capabilities = DecisionCapabilities(frozenset(QuestionKind), batching=True,
                                                     probabilities=True, confidence=True)
        decisions = registry(provider, definition=definition)
        evaluation = await decisions.evaluate("scoring", {})
        # Scoring formulas belong to providers; a score need not equal the mean.
        self.assertIs(evaluation.response.answers["q"], answer)
        provider.response = DecisionResponse({"q": ScoreResult(0.6), "ready": PredicateResult(0.9)})
        with self.assertRaises(DecisionValidationError):
            await decisions.evaluate("scoring", {})

    async def test_invalid_results_follow_explicit_failure_policy_without_partial_answers(self):
        decisions = registry(Provider(DecisionResponse({})),
                             on_error=DecisionOutcome(DecisionAction.BLOCK))
        evaluation = await decisions.evaluate("route", {})
        self.assertIsNone(evaluation.response)
        self.assertEqual(evaluation.error, "invalid_response")
        self.assertEqual(evaluation.outcome.action, DecisionAction.BLOCK)

    async def test_batched_fixture_passes_the_production_contract(self):
        definition = DecisionDefinition("batch", "2", (
            Choice("team", "Team?", {"a": "A", "b": "B"}),
            Score("quality", "Quality?", ("bad", "fair", "good")), Predicate("safe", "Safe?"),
        ))
        response = DecisionResponse({"team": ChoiceResult("a"), "quality": ScoreResult(1.5),
                                     "safe": PredicateResult(0.95)})
        fixture = FixtureDecisionProvider({("batch", "2"): response}, capabilities=CAPABILITIES)
        result = await registry(fixture, definition=definition).evaluate("batch", {"text": "private"})
        self.assertIs(result.response, response)
        changed = DecisionDefinition("batch", "3", definition.questions)
        with self.assertRaises(DecisionProviderError):
            await registry(fixture, definition=changed).evaluate("batch", {})

    async def test_provider_errors_are_private_and_never_retried(self):
        provider = Provider(error=RuntimeError("secret-token PRIVATE PROMPT"))
        with self.assertRaises(DecisionProviderError) as caught:
            await registry(provider).evaluate("route", {})
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(len(provider.calls), 1)
        result = await registry(provider, on_error=DecisionOutcome(DecisionAction.REVIEW)).evaluate("route", {})
        self.assertIsNone(result.response)
        self.assertEqual(result.outcome.action, DecisionAction.REVIEW)
        self.assertEqual(result.error, "provider_error")

    async def test_timeout_cancels_provider_and_cancellation_never_falls_back(self):
        cancelled = asyncio.Event()

        class SlowProvider(Provider):
            async def evaluate(self, request):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        decisions = registry(SlowProvider(), timeout=0.01)
        with self.assertRaises(DecisionTimeoutError):
            await decisions.evaluate("route", {})
        self.assertTrue(cancelled.is_set())
        decisions = registry(SlowProvider(), on_error=DecisionOutcome(DecisionAction.REVIEW))
        task = asyncio.create_task(decisions.evaluate("route", {}))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_context_facades_are_revoked_and_cannot_cross_invocations(self):
        active = _context(registry(), "one")
        with activate_context(active):
            saved = context.decisions
            self.assertEqual(await choose_team(), "a")
            self.assertIsNotNone((await saved.evaluate("route", {})).response)
            other = _context(registry(), "two")
            with activate_context(other):
                with self.assertRaises(context.ContextUnavailableError):
                    await saved.evaluate("route", {})
        revoke_context(active)
        with self.assertRaises(context.ContextUnavailableError):
            await saved.evaluate("route", {})

    async def test_concurrent_evaluations_do_not_share_state(self):
        class EchoProvider(Provider):
            async def evaluate(self, request):
                await asyncio.sleep(0)
                return DecisionResponse({"team": ChoiceResult(request.state["team"])})

        decisions = registry(EchoProvider())
        first, second = await asyncio.gather(decisions.evaluate("route", {"team": "a"}),
                                             decisions.evaluate("route", {"team": "b"}))
        self.assertEqual(first.response.answers["team"].value, "a")
        self.assertEqual(second.response.answers["team"].value, "b")

    async def test_telemetry_contains_only_safe_metadata(self):
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        exporter = InMemorySpanExporter()
        tracing = TracerProvider()
        tracing.add_span_processor(SimpleSpanProcessor(exporter))
        try:
            with patch("harnest.decision_runtime.get_tracer", return_value=tracing.get_tracer("test")):
                await registry().evaluate("route", {"private": "customer-secret"})
                failing = registry(Provider(error=RuntimeError("credential-secret")),
                                   on_error=DecisionOutcome(DecisionAction.REVIEW))
                await failing.evaluate("route", {})
            spans = exporter.get_finished_spans()
            self.assertEqual(len(spans), 2)
            self.assertEqual(spans[1].attributes["harnest.decision.outcome"], "provider_error")
            self.assertEqual(spans[1].events, ())
            self.assertNotIn("secret", repr([(span.attributes, span.events) for span in spans]))
        finally:
            tracing.shutdown()


def _context(decisions, invocation):
    """Construct isolated context identities for facade lifetime checks."""
    return create_agent_context(framework="adk", agent_name="agent", invocation_id=invocation,
                                user_id="user", session_id="session", metadata={},
                                resources={"decisions": decisions})
