"""Agentic Hatchet adapter backed by a separately owned runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Mapping
from typing import Any

from harnest.continuation import ContinuationConflictError
from harnest.context import context
from harnest.durable import current_native_durable_call
from harnest.extensions import Extension, ExtensionContext, extension_mutation

from .lib.client import (
    HatchetRun,
    HatchetRunStatus,
    HatchetTransport,
    open_hatchet_recovery_transport,
    open_hatchet_transport,
)
from .lib.continuations import (
    ContinuationProviderPort,
    PendingContinuation,
    invocation_continuations,
    provider_continuations,
)


_TERMINAL_FAILURE_CODES = {
    HatchetRunStatus.CANCELLED: "external_job_cancelled",
    HatchetRunStatus.FAILED: "external_job_failed",
}
_RECOVERY_PAGE_SIZE = 100
_RECOVERY_WORKFLOW_NAME = "harnest-recovery"


class HatchetContext(ExtensionContext):
    """Typed invocation view exposed through `context.extensions("hatchet")`."""

    __slots__ = ("_owner",)

    def __init__(self, plugin_name: str, owner: "HatchetExtension") -> None:
        """Bind operations to the application-owned plugin singleton."""

        super().__init__(plugin_name)
        self._owner = owner

    async def run(
        self, workflow_name: str, job_input: Mapping[str, Any]
    ) -> HatchetRun:
        """Submit external work without waiting in the agent request."""

        self._require_active()
        return await self._owner._run(workflow_name, job_input)

    async def status(self, job: HatchetRun) -> HatchetRunStatus:
        """Read one external job status through invocation credentials."""

        self._require_active()
        return await self._owner._status(job)

    async def wait(self, job: HatchetRun) -> Mapping[str, Any]:
        """Suspend this tool continuation until Hatchet reaches a terminal state."""

        self._require_active()
        return await self._owner._wait(job)

    async def cancel(self, job: HatchetRun) -> None:
        """Request cancellation while leaving worker shutdown to its owner."""

        self._require_active()
        await self._owner._cancel(job)


class HatchetExtension(Extension[HatchetContext]):
    """Expose Hatchet as an invocation-safe native plugin capability."""

    def __init__(self) -> None:
        """Initialize local ownership without connecting to Hatchet at import time."""

        self._continuations: ContinuationProviderPort | None = None
        self._application_id: str | None = None
        self._monitors: dict[str, asyncio.Task[None]] = {}
        self._started = False

    async def start(self, start_context: Any) -> None:
        """Register validation before restoring pending provider monitors."""

        self._continuations = provider_continuations(start_context)
        self._continuations.register_schema(
            "hatchet.run-result.v1", _validate_result
        )
        self._application_id = start_context.root_agent_name
        self._started = True
        await self._recover_pending(self._continuations)

    async def stop(self) -> None:
        """Stop local pollers without cancelling independently durable Hatchet jobs."""

        self._started = False
        monitors = tuple(self._monitors.values())
        for monitor in monitors:
            monitor.cancel()
        if monitors:
            await asyncio.gather(*monitors, return_exceptions=True)
        self._monitors.clear()
        self._continuations = None
        self._application_id = None

    def create_context(self, base: ExtensionContext) -> HatchetContext:
        """Create a fresh revocable view after plugin startup succeeds."""

        if not self._started:
            raise RuntimeError("Hatchet plugin is not started")
        return HatchetContext(base.extension_name, self)

    async def run(
        self, workflow_name: str, job_input: Mapping[str, Any]
    ) -> HatchetRun:
        """Submit work through the context active in the current invocation."""

        return await self.context.run(workflow_name, job_input)

    async def status(self, job: HatchetRun) -> HatchetRunStatus:
        """Read job status through the active typed context."""

        return await self.context.status(job)

    async def wait(self, job: HatchetRun) -> Mapping[str, Any]:
        """Suspend and resume the active authored tool continuation."""

        return await self.context.wait(job)

    async def cancel(self, job: HatchetRun) -> None:
        """Cancel an external run through the active typed context."""

        await self.context.cancel(job)

    async def _run(
        self, workflow_name: str, job_input: Mapping[str, Any]
    ) -> HatchetRun:
        """Submit one audited run with privacy-safe Harnest correlation metadata."""

        active = context.current()
        native = current_native_durable_call()
        idempotency_key = (
            None
            if native is None
            else native.submission_key(
                application_id=self._require_application_id(),
                user_id=active.user_id,
                session_id=active.session_id,
                run_id=active.invocation_id,
                provider="hatchet",
                capability=f"hatchet.run:{workflow_name}",
            )
        )
        transport = await open_hatchet_transport(("runs:create",))
        try:
            async with extension_mutation("hatchet", "run.create", trigger="agent"):
                return await transport.run(
                    workflow_name,
                    job_input,
                    correlation_id=active.invocation_id,
                    idempotency_key=idempotency_key,
                )
        finally:
            await transport.aclose()

    async def _status(self, job: HatchetRun) -> HatchetRunStatus:
        """Resolve a fresh read-scoped client so credentials never enter job state."""

        transport = await open_hatchet_transport(("runs:read",))
        try:
            return await transport.status(job)
        finally:
            await transport.aclose()

    async def _wait(self, job: HatchetRun) -> Mapping[str, Any]:
        """Register suspension before polling so fast jobs cannot race completion."""

        continuations = self._require_continuations()
        transport = await open_hatchet_transport(("runs:read",))
        try:
            suspended = await invocation_continuations(self.context).suspend(
                job.run_id,
                capability="hatchet.run",
                schema_id="hatchet.run-result.v1",
                validate=_validate_result,
            )
        except BaseException:
            await transport.aclose()
            raise
        await self._start_monitor(job, transport, continuations)
        return await suspended.result()

    async def _recover_pending(
        self, continuations: ContinuationProviderPort
    ) -> None:
        """Restore every provider wait through bounded keyset pages."""

        after: str | None = None
        while True:
            page = await continuations.list_pending(
                after=after, limit=_RECOVERY_PAGE_SIZE
            )
            if not page:
                return
            for pending in page:
                self._start_recovery_monitor(pending, continuations)
            # The durable continuation id is the store's ordering key; using
            # it avoids rescanning earlier rows as other replicas resolve jobs.
            after = page[-1].record.continuation_id

    def _start_recovery_monitor(
        self,
        pending: PendingContinuation,
        continuations: ContinuationProviderPort,
    ) -> None:
        """Start one recovery poller without acquiring credentials eagerly."""

        if self._monitor_active(pending.external_id):
            return
        job = HatchetRun(
            pending.external_id,
            _RECOVERY_WORKFLOW_NAME,
            pending.record.run_id,
        )
        self._track_monitor(
            job.run_id,
            self._recover_monitor(job, continuations),
        )

    async def _recover_monitor(
        self,
        job: HatchetRun,
        continuations: ContinuationProviderPort,
    ) -> None:
        """Acquire application SDK ownership and restore one provider wait."""

        try:
            transport = await open_hatchet_recovery_transport()
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._fail_if_pending(
                continuations, job.run_id, "provider_unavailable"
            )
            return
        await self._monitor(job, transport, continuations)

    async def _start_monitor(
        self,
        job: HatchetRun,
        transport: HatchetTransport,
        continuations: ContinuationProviderPort,
    ) -> None:
        """Give one local task ownership of a provider run and its transport."""

        if self._monitor_active(job.run_id):
            # Native framework replay may re-enter the tool while startup
            # recovery already owns this run; the redundant client must close.
            await transport.aclose()
            return
        self._track_monitor(
            job.run_id,
            self._monitor(job, transport, continuations),
        )

    def _track_monitor(
        self, external_id: str, awaitable: Awaitable[None]
    ) -> None:
        """Track one background poller and remove only that exact task."""

        task = asyncio.create_task(
            awaitable,
            name=f"harnest-hatchet-{external_id}",
        )
        self._monitors[external_id] = task
        task.add_done_callback(
            lambda completed: self._discard_monitor(external_id, completed)
        )

    def _monitor_active(self, external_id: str) -> bool:
        """Report whether this replica already owns a live provider poller."""

        monitor = self._monitors.get(external_id)
        return monitor is not None and not monitor.done()

    def _discard_monitor(
        self, external_id: str, completed: asyncio.Task[None]
    ) -> None:
        """Prevent an older callback from deleting a replacement monitor."""

        if self._monitors.get(external_id) is completed:
            self._monitors.pop(external_id, None)

    async def _cancel(self, job: HatchetRun) -> None:
        """Audit only the committed external cancellation request."""

        transport = await open_hatchet_transport(("runs:cancel",))
        try:
            async with extension_mutation("hatchet", "run.cancel", trigger="agent"):
                await transport.cancel(job)
        finally:
            await transport.aclose()

    async def _monitor(
        self,
        job: HatchetRun,
        transport: HatchetTransport,
        continuations: ContinuationProviderPort,
    ) -> None:
        """Translate one Hatchet terminal state into a one-shot continuation result."""

        try:
            await self._poll_until_terminal(job, transport, continuations)
        except asyncio.CancelledError:
            raise
        except ContinuationConflictError:
            # Every replica may observe the same provider terminal state; the
            # durable CAS winner already owns framework resumption.
            return
        except Exception:
            await self._fail_if_pending(
                continuations, job.run_id, "provider_unavailable"
            )
        finally:
            await transport.aclose()

    async def _poll_until_terminal(
        self,
        job: HatchetRun,
        transport: HatchetTransport,
        continuations: ContinuationProviderPort,
    ) -> None:
        """Poll with bounded cadence while the owning Harnest runtime stays active."""

        while True:
            status = await transport.status(job)
            if status is HatchetRunStatus.COMPLETED:
                result = await transport.result(job)
                await self._complete_continuation(continuations, job.run_id, result)
                return
            failure_code = _TERMINAL_FAILURE_CODES.get(status)
            if failure_code is not None:
                await self._fail_continuation(
                    continuations, job.run_id, failure_code
                )
                return
            await asyncio.sleep(0.25)

    async def _complete_continuation(
        self,
        continuations: ContinuationProviderPort,
        external_id: str,
        result: Mapping[str, Any],
    ) -> None:
        """Audit completion only after Harnest commits the continuation result."""

        async with extension_mutation(
            "hatchet", "continuation.complete", trigger="agent"
        ):
            await continuations.complete(external_id, result)

    async def _fail_continuation(
        self,
        continuations: ContinuationProviderPort,
        external_id: str,
        error_code: str,
    ) -> None:
        """Persist a stable failure code without exposing Hatchet error payloads."""

        async with extension_mutation(
            "hatchet", "continuation.fail", trigger="agent"
        ):
            await continuations.fail(external_id, error_code)

    async def _fail_if_pending(
        self,
        continuations: ContinuationProviderPort,
        external_id: str,
        error_code: str,
    ) -> None:
        """Fail an unresolved wait while accepting another replica's CAS win."""

        try:
            await self._fail_continuation(
                continuations, external_id, error_code
            )
        except ContinuationConflictError:
            return

    def _require_continuations(self) -> ContinuationProviderPort:
        """Fail closed if a call escapes the plugin's managed application lifetime."""

        if not self._started or self._continuations is None:
            raise RuntimeError("Hatchet plugin continuation provider is unavailable")
        return self._continuations

    def _require_application_id(self) -> str:
        """Return startup identity used to scope deterministic submission keys."""

        if not self._started or self._application_id is None:
            raise RuntimeError("Hatchet plugin application identity is unavailable")
        return self._application_id


def _validate_result(value: Any) -> Mapping[str, Any]:
    """Keep resumed values mapping-shaped without encoding provider payloads in state."""

    if not isinstance(value, Mapping):
        raise TypeError("Hatchet continuation result must be a mapping")
    return dict(value)


extension = HatchetExtension()
# The domain-facing name reads naturally while retaining the required export.
hatchet = extension


__all__ = [
    "HatchetContext",
    "HatchetExtension",
    "HatchetRun",
    "HatchetRunStatus",
    "hatchet",
    "extension",
]
