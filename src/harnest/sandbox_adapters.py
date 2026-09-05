"""Lower portable sandboxes into each framework's native execution mechanism."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from .sandbox_control import execution_control
from .sandbox_runtime import SandboxProviderContractError, SandboxRuntime
from .sandbox_types import (
    SandboxBackend, SandboxContext, SandboxFile, SandboxRequest, SandboxResult,
    sandbox_metadata_to_dict,
)


SANDBOX_TOOL_NAME = "harnest_execute_python"


def adk_executor(definition: Any) -> Any:
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


def langchain_tool(definition: Any, *, name: str = SANDBOX_TOOL_NAME) -> Any:
    """Expose isolated Python through LangGraph's existing native tool loop."""
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel, ConfigDict, Field

    class Arguments(BaseModel):
        """Reject undeclared authority and non-text model arguments."""

        model_config = ConfigDict(extra="forbid", strict=True)
        code: str = Field(min_length=1)

    runner = _PortableToolRunner(definition, "langgraph")

    def execute(code: str) -> dict[str, Any]:
        """Run Python in the configured provider, not the agent server process."""
        return runner.execute(code)

    async def aexecute(code: str) -> dict[str, Any]:
        """Preserve invocation context while keeping blocking SDKs off the loop."""
        return await runner.aexecute(code)

    return StructuredTool.from_function(
        func=execute, coroutine=aexecute, name=name,
        description=f"Execute Python in the assigned sandbox {name}. "
        "Use print() to return output; results include stdout, stderr, and output_files.",
        args_schema=Arguments,
    )


class _PortableToolRunner:
    """Share authority, identity, and cancellation policy across native tools."""

    def __init__(self, definition: Any, framework: str) -> None:
        """Allocate a lazy runtime per assigned tool, without starting a provider."""
        self.definition = definition
        self.runtime = SandboxRuntime(definition, framework)

    def execute(self, code: str, native: Any = None) -> dict[str, Any]:
        """Execute only code; identity and metadata remain application-owned."""
        with execution_control(self.definition.timeout_seconds):
            request = SandboxRequest(
                code=code, timeout_seconds=self.definition.timeout_seconds,
                context=execution_context(native), metadata=self.definition.metadata,
                network_policy=self.definition.network_policy,
            )
            result = self.runtime.run(lambda backend: _execute_portable(backend, request))
            return _tool_result(result)

    async def aexecute(self, code: str, native: Any = None) -> dict[str, Any]:
        """Revoke detached workers when a framework tool call is cancelled."""
        # Cancellation of to_thread does not cancel the worker; its inherited
        # control must be revoked before queued provider admission can succeed.
        with execution_control(self.definition.timeout_seconds) as control:
            try:
                return await asyncio.to_thread(self.execute, code, native)
            except asyncio.CancelledError:
                control.cancelled.set()
                raise


def _execute_portable(backend: Any, request: SandboxRequest) -> SandboxResult:
    """Reject native ADK-only factories rather than inventing an invocation context."""
    if not isinstance(backend, SandboxBackend):
        raise SandboxProviderContractError()
    result = backend.execute(request)
    if not isinstance(result, SandboxResult):
        raise SandboxProviderContractError()
    return result


def execution_context(native: Any = None) -> SandboxContext:
    """Prefer Harnest identity and adapt native ADK identity outside its runtime."""
    from .context import optional_active_context

    active = optional_active_context()
    if active is None:
        session = getattr(native, "session", None)
        return SandboxContext(
            agent_name=getattr(getattr(native, "agent", None), "name", None),
            invocation_id=getattr(native, "invocation_id", None),
            user_id=getattr(session, "user_id", None),
            session_id=getattr(session, "id", None),
        )
    return SandboxContext(
        agent_name=active.agent_name, invocation_id=active.invocation_id,
        user_id=active.user_id, session_id=active.session_id,
    )


def _portable_file(value: Any) -> SandboxFile:
    """Retain file bytes and metadata without opening filesystem paths."""
    return SandboxFile(value.name, value.content, value.mime_type)


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


def _tool_result(result: SandboxResult) -> dict[str, Any]:
    """Serialize provider output as a native LangChain tool result, including files."""
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "status": result.status.value,
        "exit_code": result.exit_code,
        "metadata": sandbox_metadata_to_dict(result.metadata),
        "output_files": [
            {
                "name": value.name, "mime_type": value.mime_type,
                "content": base64.b64encode(value.content).decode("ascii")
                if isinstance(value.content, bytes) else value.content,
            }
            for value in result.output_files
        ],
    }
