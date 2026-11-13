"""Lower portable sandboxes into each framework's native execution mechanism."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from .sandbox_control import execution_control
from .sandbox_runtime import SandboxProviderContractError, SandboxRuntime
from .sandbox_types import (
    SandboxBackend, SandboxContext, SandboxFile, SandboxRequest, SandboxResult,
    sandbox_metadata_to_dict,
)


SANDBOX_TOOL_NAME = "harnest_execute_python"


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
