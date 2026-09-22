"""Decision disclosure, invocation isolation and streaming ownership boundaries."""

import asyncio
from dataclasses import replace
import json
import unittest

from test_decisions import registry
from test_lifecycle_runtime import FakeDriver, request

from harnest import context
from harnest.context import ContextValue, activate_agent_scope
from harnest.checkpoint import DurableRunResult
from harnest.decision_output import DecisionOutput
from harnest.decisions import (
    ChoiceResult, DecisionEvaluation, DecisionResponse, PredicateResult, ScoreResult,
)
from harnest.lifecycle_runtime import LifecycleRuntimeDriver
from harnest.output import OutputPolicy
from harnest.runtime_contract import InvocationResult
from harnest.runtime_continuation import public_output
from harnest.runtime_sse import stream_frame


class DecisionDriver(FakeDriver):
    """Exercise decisions in child agent scopes and record generator cleanup."""

    async def invoke(self, request):
        """Use hidden decision values to produce a public answer."""
        await asyncio.sleep(0)
        with activate_agent_scope(request.invocation_id):
            result = await context.decisions.evaluate("route", {"secret": "private-ticket"})
        answer = result.response.answers["team"].value
        return InvocationResult(text=answer, events=({"type": "message", "text": answer},),
                                result=None, session_id=request.session_id, metadata={})

    async def stream(self, request):
        """Perform a decision before the answer and another just before EOF."""
        try:
            result = await self.invoke(request)
            yield result.events[0]
            await context.decisions.evaluate("route", {})
        finally:
            self.closed = True


def driver(*, enabled):
    """Bind a fixture resource through the normal lifecycle ownership boundary."""
    return LifecycleRuntimeDriver(
        DecisionDriver(), (), output_policy=OutputPolicy(decision_results=enabled),
        context_values=(ContextValue("decisions", registry(), "test"),),
    )


class DecisionOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_hidden_decisions_still_drive_answers(self):
        runtime = driver(enabled=False)
        result = await runtime.invoke(request())
        events = [event async for event in runtime.stream(request())]
        self.assertEqual(result.text, "a")
        self.assertEqual([event["type"] for event in events], ["message"])
        self.assertNotIn("private-ticket", json.dumps(public_output(result.events)))
        await runtime.close()

    async def test_visible_decisions_are_ordered_once_and_flush_at_eof(self):
        runtime = driver(enabled=True)
        events = [event async for event in runtime.stream(request())]
        self.assertEqual([event["type"] for event in events], ["decision_result", "message", "decision_result"])
        self.assertEqual(events[0]["agent"], "invoke-1")
        self.assertEqual(events[0]["value"]["answers"]["team"]["value"], "a")
        self.assertNotIn("private-ticket", json.dumps(events))
        self.assertTrue(runtime._driver.closed)
        frame, payload = stream_frame(events[0], sequence=1, response_id="r", session_id="s")
        self.assertEqual(frame, "response.decision_result")
        self.assertEqual(payload["value"], public_output(events)[0]["value"])
        saved = DurableRunResult.capture("a", events, None, {})
        self.assertEqual(saved.output[0]["value"], events[0]["value"])
        self.assertNotIn("private-ticket", json.dumps(saved.as_dict()))
        await runtime.close()

    async def test_parallel_invocations_do_not_share_results(self):
        runtime = driver(enabled=True)
        first, second = await asyncio.gather(
            runtime.invoke(replace(request(), invocation_id="first")),
            runtime.invoke(replace(request(), invocation_id="second")),
        )
        self.assertEqual([e["agent"] for e in first.events if e["type"] == "decision_result"], ["first"])
        self.assertEqual([e["agent"] for e in second.events if e["type"] == "decision_result"], ["second"])
        await runtime.close()

    async def test_stream_close_revokes_context_and_closes_backend(self):
        runtime = driver(enabled=True)
        stream = runtime.stream(request())
        self.assertEqual((await anext(stream))["type"], "decision_result")
        with self.assertRaises(context.ContextUnavailableError):
            context.current()
        await stream.aclose()
        self.assertTrue(runtime._driver.closed)
        await runtime.close()

    async def test_all_answer_shapes_are_json_and_detached(self):
        response = DecisionResponse({
            "choice": ChoiceResult("a", {"a": 0.7, "b": 0.3}, 0.7),
            "score": ScoreResult(0.5, (0.5, 0.5), 0.8),
            "predicate": PredicateResult(0.9),
        })
        evaluation = DecisionEvaluation("route", "1", "fixture", "1", response, None, 0.1)
        output = DecisionOutput(enabled=True)
        output.record(evaluation, agent="test")
        value = json.loads(json.dumps(output.drain()))[0]["value"]
        self.assertEqual(value["answers"]["predicate"], {"kind": "predicate", "probability": 0.9})
        self.assertEqual(value["answers"]["score"]["probabilities"], [0.5, 0.5])
        value["answers"]["choice"]["probabilities"]["a"] = 0
        self.assertEqual(response.answers["choice"].probabilities["a"], 0.7)
        self.assertEqual(output.drain(), [])
