"""Scoped compatibility for native ADK code-execution continuation."""

from __future__ import annotations

from contextlib import aclosing
import json
from typing import Any

from ...sandbox_adapters import _execute_portable, _portable_file, execution_context
from ...sandbox_control import execution_control
from ...sandbox_runtime import SandboxRuntime
from ...sandbox_types import (
    SandboxBackend,
    SandboxFile,
    SandboxRequest,
    SandboxResult,
    sandbox_metadata_to_dict,
)


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


def code_executor(definition: Any) -> Any:
    """Use ADK's code-execution loop without constructing a sandbox at compile time."""
    from google.adk.code_executors import BaseCodeExecutor
    from pydantic import PrivateAttr

    class LazySandboxExecutor(BaseCodeExecutor):
        """Adapt native code requests while retaining ADK-only provider support."""

        _runtime: Any = PrivateAttr()

        def execute_code(self, invocation_context: Any, code_execution_input: Any) -> Any:
            """Delegate only actual model-generated code to the lazy provider."""
            with execution_control(self.timeout_seconds):
                return self._runtime.run(
                    lambda backend: _execute_adk(
                        backend, invocation_context, code_execution_input,
                        self.timeout_seconds, self._runtime.definition.metadata,
                        self._runtime.definition.network_policy,
                    )
                )

    # ADK parses code with the adapter's fields, not the hidden provider's
    # fields; retain supported native parsing/retry options at this boundary.
    executor = LazySandboxExecutor(
        timeout_seconds=definition.timeout_seconds,
        **dict(definition._executor_options),
    )
    executor._runtime = SandboxRuntime(definition, "adk")
    return executor

def _execute_adk(
    backend: Any,
    context: Any,
    source: Any,
    timeout: int | None,
    metadata: Any,
    network_policy: Any,
) -> Any:
    """Translate neutral providers but pass legacy ADK context through unchanged."""
    if not isinstance(backend, SandboxBackend):
        return backend.execute_code(context, source)
    request = SandboxRequest(
        code=source.code,
        timeout_seconds=timeout,
        context=execution_context(context),
        input_files=tuple(_portable_file(value) for value in source.input_files),
        execution_id=source.execution_id,
        metadata=metadata,
        network_policy=network_policy,
    )
    return _adk_result(_execute_portable(backend, request))

def _adk_file(value: SandboxFile) -> Any:
    """Let ADK retain ownership of native artifact handling."""
    from google.adk.code_executors.code_execution_utils import File

    return File(value.name, value.content, value.mime_type)

def _adk_result(result: SandboxResult) -> Any:
    """Return the result type consumed by ADK's code-execution processor."""
    from google.adk.code_executors.code_execution_utils import CodeExecutionResult

    metadata = sandbox_metadata_to_dict(result.metadata)
    # Native ADK results have no metadata field consumed by their event loop.
    # A JSON envelope preserves explicitly returned provider properties for
    # the model and event history, without changing stderr/retry semantics.
    stdout = json.dumps({"stdout": result.stdout, "metadata": metadata}) if metadata else result.stdout
    native = CodeExecutionResult(
        stdout=stdout, stderr=result.stderr,
        output_files=[_adk_file(value) for value in result.output_files],
    )
    native.metadata = metadata
    return native
