"""Guard the narrow ADK consumed-code STOP compatibility boundary."""

import unittest
from contextlib import aclosing

from google.adk.agents import LlmAgent
from google.adk.events import Event
from google.adk.flows.llm_flows import _code_execution
from google.adk.models import LlmResponse
from google.genai import types

from harnest.sandbox_adk import _CodeContinuationProcessor, sandbox_agent_type


def _result_event():
    """Represent native execution output without running any provider."""
    return Event(author="probe", content=types.Content(parts=[types.Part(
        code_execution_result=types.CodeExecutionResult(outcome="OUTCOME_OK", output="42"),
    )]))


def _response(**kwargs):
    """Supply realistic terminal model metadata for the processor boundary."""
    return LlmResponse(
        content=types.Content(parts=[types.Part(text="```python\nprint(42)\n```")]),
        finish_reason=types.FinishReason.STOP, **kwargs,
    )


class NativeProcessor:
    """Simulate only native response consumption, retaining observable event identity."""

    def __init__(self, *, consume=True, events=(), error=False):
        """Describe one bounded processor outcome for the guard tests."""
        self.consume, self.events, self.error = consume, events, error
        self.closed = False

    async def run_async(self, context, response):
        """Mirror native yield-before-consumption ordering and generator cleanup."""
        try:
            for event in self.events:
                yield event
            if self.error:
                raise RuntimeError("native failure")
            if self.consume:
                response.content = None
        finally:
            self.closed = True


class SandboxADKCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    """Preserve native failures, event metadata, and non-sandbox flow ownership."""

    async def test_consumed_stop_keeps_native_events_and_continues(self):
        """Change only discarded response termination, never emitted result metadata."""
        event = _result_event()
        response = _response()
        delegate = NativeProcessor(events=(event,))
        emitted = [item async for item in _CodeContinuationProcessor(delegate).run_async(None, response)]
        self.assertIs(emitted[0], event)
        self.assertIsNone(response.finish_reason)
        self.assertIsNone(response.error_code)
        self.assertTrue(delegate.closed)

    async def test_unconsumed_and_error_responses_retain_stop(self):
        """Never suppress genuine empty answers, native errors, or exhausted retries."""
        cases = (
            (NativeProcessor(events=(_result_event(),), consume=False), _response()),
            (NativeProcessor(), _response()),
            (NativeProcessor(events=(_result_event(),)), LlmResponse(finish_reason="STOP")),
            (NativeProcessor(events=(_result_event(),)), _response(error_code="native-error")),
            (NativeProcessor(consume=False), _response(partial=True)),
        )
        for delegate, response in cases:
            with self.subTest(response=response):
                async for _ in _CodeContinuationProcessor(delegate).run_async(None, response):
                    pass
                self.assertEqual(response.finish_reason, types.FinishReason.STOP)

    async def test_other_finish_reasons_are_not_normalized(self):
        """Keep truncation and safety outcomes even if a native processor consumes content."""
        for reason in (types.FinishReason.MAX_TOKENS, types.FinishReason.SAFETY):
            response = _response()
            response.finish_reason = reason
            processor = _CodeContinuationProcessor(NativeProcessor(events=(_result_event(),)))
            async for _ in processor.run_async(None, response):
                pass
            self.assertEqual(response.finish_reason, reason)

    async def test_native_failure_and_cancellation_close_the_delegate(self):
        """Do not alter continuation when native processing fails or is interrupted."""
        response = _response()
        failed = NativeProcessor(error=True)
        with self.assertRaisesRegex(RuntimeError, "native failure"):
            async for _ in _CodeContinuationProcessor(failed).run_async(None, response):
                pass
        cancelled = NativeProcessor(events=(_result_event(),))
        async with aclosing(_CodeContinuationProcessor(cancelled).run_async(None, response)) as stream:
            await anext(stream)
        self.assertTrue(failed.closed)
        self.assertTrue(cancelled.closed)
        self.assertEqual(response.finish_reason, types.FinishReason.STOP)

    def test_wrapper_is_scoped_to_fresh_sandbox_flows(self):
        """Leave shared native processors and ordinary agents completely unchanged."""
        plain = LlmAgent(name="plain", model="gemini-2.5-flash")
        adapted = sandbox_agent_type(LlmAgent)(name="sandbox", model="gemini-2.5-flash")
        first, second = adapted._llm_flow, adapted._llm_flow
        self.assertIsNot(first, second)
        self.assertIn(_code_execution.response_processor, plain._llm_flow.response_processors)
        self.assertNotIn(_code_execution.response_processor, first.response_processors)
        wrapper = next(item for item in first.response_processors if isinstance(item, _CodeContinuationProcessor))
        self.assertIs(wrapper._native, _code_execution.response_processor)


if __name__ == "__main__":
    unittest.main()
