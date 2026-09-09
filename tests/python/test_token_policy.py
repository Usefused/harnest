"""Token policy contracts independent of framework and provider services."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import unittest

from harnest.lifecycle import LifecycleContext
from harnest.model_hooks import model_invocation_scope
from harnest.token_reduction import reduce_request
from harnest.token_runtime import prepare, prepare_sync
from harnest.tokens import (
    TokenBudgetExceeded, TokenCount, TokenPolicy, TokenPolicyError, TokenRequest,
    token_policy_scope, _policy_for, _STATE,
)


def request():
    """Build a transcript with a completed tool exchange and a new human turn."""
    return TokenRequest("langgraph", "test", (
        {"type": "system", "data": {"content": "pinned"}},
        {"type": "human", "data": {"content": "old"}},
        {"type": "ai", "data": {"content": "", "tool_calls": [{"id": "a"}]}},
        {"type": "tool", "data": {"content": "x" * 100, "tool_call_id": "a"}},
        {"type": "human", "data": {"content": "new"}},
    ), system="instructions", tools=({"name": "lookup"}, {"name": "search"}))


def exact_count(_request):
    """Supply an exact test counter independent of provider tokenizers."""
    return TokenCount(100, estimated=False)


class TokenPolicyTests(unittest.TestCase):
    def test_validation_rejects_invalid_controls(self):
        for kwargs in (
            {"enforce": "true"}, {"max_input_tokens": True},
            {"keep_recent_turns": 0}, {"count_tokens": "estimate"},
            {"reserve_output_tokens": -1},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises((ValueError, TypeError)):
                TokenPolicy(**kwargs)

    def test_observation_preserves_request_and_reports_would_exceed(self):
        reports = []
        policy = TokenPolicy(max_input_tokens=50, count_tokens=exact_count, observer=reports.append)
        original = request()
        call = prepare_sync(original, policy, "agent")
        self.assertEqual(call.request, original)
        self.assertEqual(reports[0].exceeded, ("input_tokens",))
        self.assertFalse(reports[0].input_before.estimated)
        self.assertNotIn("instructions", repr(reports))

    def test_enforcement_stops_before_reservation(self):
        reports = []
        policy = TokenPolicy(enforce=True, max_input_tokens=50, count_tokens=exact_count, observer=reports.append)
        with token_policy_scope(policy):
            with self.assertRaises(TokenBudgetExceeded) as failure:
                prepare_sync(request(), policy, "agent")
            self.assertEqual(failure.exception.budget, "input_tokens")
            self.assertEqual(_STATE.get().calls, {})
            self.assertEqual(reports[0].phase, "blocked")

    def test_context_budget_reserves_output_capacity(self):
        policy = TokenPolicy(enforce=True, context_window=120, max_output_tokens=21, count_tokens=exact_count)
        with self.assertRaisesRegex(TokenBudgetExceeded, "context_window"):
            prepare_sync(request(), policy, "agent")
        prepare_sync(request(), replace(policy, max_output_tokens=20), "agent")

    def test_recent_turns_keep_instructions_and_whole_tool_exchanges(self):
        original = request()
        reduced = reduce_request(original, TokenPolicy(keep_recent_turns=1))
        self.assertEqual([m["type"] for m in reduced.messages], ["system", "human"])
        self.assertEqual(len(original.messages), 5)
        current_tool_turn = replace(original, messages=original.messages[:-1])
        self.assertEqual(reduce_request(current_tool_turn, TokenPolicy(keep_recent_turns=1)), current_tool_turn)

    def test_adk_function_response_is_not_a_new_user_turn(self):
        native = TokenRequest("adk", "test", (
            {"role": "user", "parts": [{"text": "old"}]},
            {"role": "model", "parts": [{"text": "reply"}]},
            {"role": "user", "parts": [{"text": "new"}]},
            {"role": "model", "parts": [{"function_call": {"name": "lookup"}}]},
            {"role": "user", "parts": [{"function_response": {"name": "lookup", "response": {"text": "x" * 100, "count": 9}}}]},
        ))
        result = reduce_request(native, TokenPolicy(keep_recent_turns=1, max_tool_result_chars=20))
        self.assertEqual(len(result.messages), 3)
        payload = result.messages[-1]["parts"][0]["function_response"]["response"]
        self.assertEqual(len(payload["text"]), 20)
        self.assertEqual(payload["count"], 9)
        self.assertEqual(len(native.messages[-1]["parts"][0]["function_response"]["response"]["text"]), 100)

    def test_tool_text_reduction_preserves_ids_and_media(self):
        original = request()
        result = reduce_request(original, TokenPolicy(max_tool_result_chars=20))
        tool = result.messages[3]["data"]
        self.assertEqual(tool["tool_call_id"], "a")
        self.assertEqual(len(tool["content"]), 20)
        self.assertIn("truncated", tool["content"])
        self.assertEqual(len(original.messages[3]["data"]["content"]), 100)

    def test_custom_transform_cannot_mutate_original_or_grant_tools(self):
        original = request()
        def mutate(value):
            """Exercise intentionally mutable strategy input."""
            value.messages[-1]["data"]["content"] = "summary"
            return value
        policy = TokenPolicy(request_transform=mutate, tool_selector=lambda _r: ["search"])
        call = prepare_sync(original, policy, "agent")
        self.assertEqual(call.request.messages[-1]["data"]["content"], "summary")
        self.assertEqual(original.messages[-1]["data"]["content"], "new")
        self.assertEqual(call.tools_removed, 1)
        with self.assertRaises(TokenPolicyError):
            prepare_sync(original, replace(policy, tool_selector=lambda _r: ["invented"]), "agent")

    def test_sync_rejects_async_strategies(self):
        async def transform(value):
            """Model an asynchronous summarizer."""
            return value
        with self.assertRaisesRegex(TokenPolicyError, "async token strategies"):
            prepare_sync(request(), TokenPolicy(request_transform=transform), "agent")

    def test_trusted_overrides_restore_and_disable_without_metadata(self):
        default = TokenPolicy()
        override = TokenPolicy(max_input_tokens=10)
        self.assertIs(_policy_for(default, "a"), default)
        with token_policy_scope(override):
            self.assertIs(_policy_for(default, "a"), override)
            with token_policy_scope(None, agent_name="a"):
                self.assertIsNone(_policy_for(default, "a"))
                self.assertIs(_policy_for(default, "b"), override)
        self.assertIs(_policy_for(default, "a"), default)

    def test_model_call_reservations_are_atomic_and_per_agent(self):
        policy = TokenPolicy(enforce=True, max_model_calls=2)
        with token_policy_scope(policy):
            state = _STATE.get()
            def reserve(_index):
                """Race independent workers against one shared call budget."""
                try:
                    return state.reserve("a", policy)
                except TokenBudgetExceeded:
                    return None
            with ThreadPoolExecutor(max_workers=8) as executor:
                admitted = list(executor.map(reserve, range(20)))
            self.assertEqual(sorted(x for x in admitted if x is not None), [1, 2])
            self.assertEqual(state.reserve("b", policy), 1)

    def test_stream_advancements_reuse_state_and_invocations_reset(self):
        policy = TokenPolicy(enforce=True, max_model_calls=1)
        ctx = LifecycleContext("adk", "agent", "i", "u", "s")
        with model_invocation_scope(ctx):
            prepare_sync(request(), policy, "agent")
        self.assertIsNone(_STATE.get())
        with model_invocation_scope(ctx), self.assertRaises(TokenBudgetExceeded):
            prepare_sync(request(), policy, "agent")
        other = LifecycleContext("adk", "agent", "j", "u", "s")
        with model_invocation_scope(other):
            prepare_sync(request(), policy, "agent")


class AsyncTokenPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_strategies_and_observers(self):
        reports = []
        async def transform(value):
            """Simulate a custom summarizer without calling a provider."""
            await asyncio.sleep(0)
            return replace(value, messages=value.messages[-1:])
        async def observer(report):
            """Accept reports in an asynchronous sink."""
            reports.append(report)
        policy = TokenPolicy(request_transform=transform, observer=observer)
        result = await prepare(request(), policy, "agent")
        self.assertEqual(len(result.request.messages), 1)
        self.assertEqual(reports[0].messages_removed, 4)

    async def test_concurrent_invocations_do_not_share_budgets(self):
        policy = TokenPolicy(enforce=True, max_model_calls=1)
        async def invoke():
            """Own one independent request context across a scheduling boundary."""
            with token_policy_scope(policy):
                await asyncio.sleep(0)
                return (await prepare(request(), policy, "agent")).calls
        self.assertEqual(await asyncio.gather(invoke(), invoke()), [1, 1])
