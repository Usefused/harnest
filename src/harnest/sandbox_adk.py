"""Scoped compatibility for native ADK code-execution continuation."""

from __future__ import annotations

from contextlib import aclosing
from typing import Any

from .sandbox_control import execution_control


def sandbox_agent_type(native_type: type) -> type:
    """Retain the native agent and flow while adapting only its code processor."""

    class SandboxLlmAgent(native_type):
        """Keep consumed code responses distinct from empty final model responses."""

        @property
        def _llm_flow(self) -> Any:
            """Wrap this flow's processor without mutating shared ADK instances."""
            from google.adk.flows.llm_flows import _code_execution

            flow = super()._llm_flow
            flow.response_processors = [
                _CodeContinuationProcessor(processor)
                if processor is _code_execution.response_processor else processor
                for processor in flow.response_processors
            ]
            return flow

    return SandboxLlmAgent


class _CodeContinuationProcessor:
    """Delegate execution, events, artifacts, retries, and cleanup to native ADK."""

    def __init__(self, native: Any) -> None:
        """Hold the stateless native processor without modifying its behavior."""
        self._native = native

    async def run_async(self, invocation_context: Any, llm_response: Any) -> Any:
        """Normalize STOP only after native execution consumes its model response."""
        from google.genai import types

        had_content = bool(llm_response.content and llm_response.content.parts)
        executed = False
        executor = getattr(getattr(invocation_context, "agent", None), "code_executor", None)
        with execution_control(getattr(executor, "timeout_seconds", None)) as control:
            completed = False
            try:
                async with aclosing(self._native.run_async(invocation_context, llm_response)) as events:
                    async for event in events:
                        executed = executed or _has_code_result(event)
                        yield event
                completed = True
            finally:
                # Native ADK uses to_thread internally; closing its generator
                # must revoke that worker even when its asyncio future is gone.
                if not completed:
                    control.cancelled.set()
        if (
            had_content and executed and llm_response.content is None
            and llm_response.finish_reason == types.FinishReason.STOP
            and llm_response.error_code is None
        ):
            # ADK 2.8 consumes code content to request the next model turn, but
            # its subsequent empty-STOP guard mistakes that for an empty final.
            # Only the discarded intermediate response loses STOP; actual
            # final responses, native errors, and exhausted retries stay intact.
            llm_response.finish_reason = None


def _has_code_result(event: Any) -> bool:
    """Require an actual native code-result event before changing continuation."""
    content = event.content
    return bool(content and any(part.code_execution_result is not None for part in content.parts or []))
