"""Typed authoring surface for the official Hatchet Harnest Extension."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from harnest.extensions import Extension, ExtensionContext

class HatchetRunStatus(str, Enum):
    """Provider statuses that affect continuation decisions."""

    QUEUED: "HatchetRunStatus"
    RUNNING: "HatchetRunStatus"
    COMPLETED: "HatchetRunStatus"
    CANCELLED: "HatchetRunStatus"
    FAILED: "HatchetRunStatus"

@dataclass(frozen=True)
class HatchetRun:
    """Portable identity for work owned by the external Hatchet runtime."""

    run_id: str
    workflow_name: str
    correlation_id: str

class HatchetContext(ExtensionContext):
    """Typed invocation view exposed by the installed Hatchet extension."""

    async def run(
        self, workflow_name: str, job_input: Mapping[str, Any]
    ) -> HatchetRun:
        """Submit external work without waiting in the agent request."""

        ...

    async def status(self, job: HatchetRun) -> HatchetRunStatus:
        """Read one external job status through invocation credentials."""

        ...

    async def wait(self, job: HatchetRun) -> Mapping[str, Any]:
        """Suspend the current durable tool until the job becomes terminal."""

        ...

    async def cancel(self, job: HatchetRun) -> None:
        """Request cancellation of an external Hatchet run."""

        ...

class HatchetExtension(Extension[HatchetContext]):
    """Expose Hatchet as an invocation-safe extension capability."""

    async def start(self, start_context: Any) -> None:
        """Start recovery and bind application continuation authority."""

        ...

    async def stop(self) -> None:
        """Stop local recovery monitors without cancelling durable jobs."""

        ...

    async def run(
        self, workflow_name: str, job_input: Mapping[str, Any]
    ) -> HatchetRun:
        """Submit external work through the active invocation context."""

        ...

    async def status(self, job: HatchetRun) -> HatchetRunStatus:
        """Read an external job status through the active context."""

        ...

    async def wait(self, job: HatchetRun) -> Mapping[str, Any]:
        """Suspend and resume the active durable tool continuation."""

        ...

    async def cancel(self, job: HatchetRun) -> None:
        """Cancel an external job through the active context."""

        ...

extension: HatchetExtension
hatchet: HatchetExtension
