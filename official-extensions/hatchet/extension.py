"""Agentic Hatchet adapter backed by a separately owned runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
import logging
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
from .lib.payloads import normalize_json_mapping


_TERMINAL_FAILURE_CODES = {
    HatchetRunStatus.CANCELLED: "external_job_cancelled",
    HatchetRunStatus.FAILED: "external_job_failed",
}
_RECOVERY_PAGE_SIZE = 100
_RECOVERY_WORKFLOW_NAME = "harnest-recovery"
_RECOVERY_CONCURRENCY = 16
_POLL_INITIAL_SECONDS = 0.25
_POLL_MAX_SECONDS = 5.0
_RETRY_INITIAL_SECONDS = 1.0
_RETRY_MAX_SECONDS = 30.0
_LOGGER = logging.getLogger("harnest.extension.hatchet")


class HatchetContext(ExtensionContext):
    """Typed invocation view exposed through `context.extensions("hatchet")`."""

    __slots__ = ("_owner",)

    def __init__(self, plugin_name: str, owner: "HatchetExtension") -> None:
        """Bind operations to the application-owned extension singleton."""

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
    """Expose Hatchet as an invocation-safe native extension capability."""

    def __init__(self) -> None:
        """Initialize local ownership without connecting to Hatchet at import time."""

        self._continuations: ContinuationProviderPort | None = None
        self._application_id: str | None = None
        self._monitors: dict[str, asyncio.Task[None]] = {}
        self._recovery_slots: asyncio.Semaphore | None = None
        self._started = False

    async def start(self, start_context: Any) -> None:
        """Register validation before restoring pending provider monitors."""

        if self._started:
            raise RuntimeError("Hatchet extension is already started")
        self._continuations = provider_continuations(start_context)
        self._continuations.register_schema(
            "hatchet.run-result.v1", _validate_result
        )
        application_id = getattr(start_context, "root_agent_name", None)
        if not isinstance(application_id, str) or not application_id.strip():
            self._continuations = None
            raise RuntimeError("Hatchet extension requires an application identity")
        self._application_id = application_id
        self._recovery_slots = asyncio.Semaphore(_RECOVERY_CONCURRENCY)
        self._started = True
        try:
            await self._recover_pending(self._continuations)
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        """Stop local pollers without cancelling independently durable Hatchet jobs."""

        self._started = False
        monitors = tuple(self._monitors.values())
        for monitor in monitors:
            monitor.cancel()
        if monitors:
            await asyncio.gather(*monitors, return_exceptions=True)
        self._monitors.clear()
        self._recovery_slots = None
        self._continuations = None
        self._application_id = None

    def create_context(self, base: ExtensionContext) -> HatchetContext:
        """Create a fresh revocable view after extension startup succeeds."""

        if not self._started:
            raise RuntimeError("Hatchet extension is not started")
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
            await self._close_monitor_transport(transport)

    async def _status(self, job: HatchetRun) -> HatchetRunStatus:
        """Resolve a fresh read-scoped client so credentials never enter job state."""

        transport = await open_hatchet_transport(("runs:read",))
        try:
            return await transport.status(job)
        finally:
            await self._close_monitor_transport(transport)

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
            await self._close_monitor_transport(transport)
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
        """Poll recovery fairly without retaining one client per pending wait."""

        slots = self._recovery_slots
        if slots is None:
            return

        async def observe() -> HatchetRunStatus:
            """Hold scarce recovery ownership for only one provider observation."""

            async with slots:
                if not self._started:
                    raise asyncio.CancelledError
                transport = await open_hatchet_recovery_transport()
                try:
                    return await self._poll_once(job, transport, continuations)
                finally:
                    await self._close_monitor_transport(transport)

        try:
            await self._poll_until_terminal(observe)
        except ContinuationConflictError:
            # A different replica already committed this terminal observation.
            return

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
            await self._close_monitor_transport(transport)
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
            await self._close_monitor_transport(transport)

    async def _monitor(
        self,
        job: HatchetRun,
        transport: HatchetTransport,
        continuations: ContinuationProviderPort,
    ) -> None:
        """Translate one Hatchet terminal state into a one-shot continuation result."""

        try:
            await self._poll_until_terminal(
                lambda: self._poll_once(job, transport, continuations)
            )
        except asyncio.CancelledError:
            raise
        except ContinuationConflictError:
            # Every replica may observe the same provider terminal state; the
            # durable CAS winner already owns framework resumption.
            return
        finally:
            await self._close_monitor_transport(transport)

    async def _poll_until_terminal(
        self,
        observe: Callable[[], Awaitable[HatchetRunStatus]],
    ) -> None:
        """Poll fairly and retain durable waits across transient provider outages."""

        prior_status: HatchetRunStatus | None = None
        poll_delay = _POLL_INITIAL_SECONDS
        retry_attempt = 0
        while True:
            try:
                status = await observe()
            except asyncio.CancelledError:
                raise
            except ContinuationConflictError:
                raise
            except Exception:
                retry_attempt += 1
                await _retry_pause("monitor operation", retry_attempt)
                continue
            retry_attempt = 0
            if status in _TERMINAL_FAILURE_CODES or status is HatchetRunStatus.COMPLETED:
                return
            if status is not prior_status:
                poll_delay = _POLL_INITIAL_SECONDS
                prior_status = status
            await asyncio.sleep(poll_delay)
            poll_delay = min(poll_delay * 2, _POLL_MAX_SECONDS)

    async def _close_monitor_transport(
        self, transport: HatchetTransport
    ) -> None:
        """Close a provider client without retaining or propagating SDK failures."""

        try:
            await transport.aclose()
        except Exception:
            # Cleanup diagnostics must not retain SDK exceptions, which can
            # include request headers or credential-bearing endpoint text.
            _LOGGER.warning("Hatchet transport cleanup failed")

    async def _poll_once(
        self,
        job: HatchetRun,
        transport: HatchetTransport,
        continuations: ContinuationProviderPort,
    ) -> HatchetRunStatus:
        """Translate one provider observation without resolving on transport errors."""

        status = await transport.status(job)
        if status is HatchetRunStatus.COMPLETED:
            result = await transport.result(job)
            try:
                normalized = _validate_result(result)
            except (TypeError, ValueError):
                await self._fail_continuation(
                    continuations, job.run_id, "invalid_external_result"
                )
                return status
            await self._complete_continuation(
                continuations, job.run_id, normalized
            )
        failure_code = _TERMINAL_FAILURE_CODES.get(status)
        if failure_code is not None:
            await self._fail_continuation(
                continuations, job.run_id, failure_code
            )
        return status

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

    def _require_continuations(self) -> ContinuationProviderPort:
        """Fail closed outside the extension's managed application lifetime."""

        if not self._started or self._continuations is None:
            raise RuntimeError("Hatchet extension continuation provider is unavailable")
        return self._continuations

    def _require_application_id(self) -> str:
        """Return startup identity used to scope deterministic submission keys."""

        if not self._started or self._application_id is None:
            raise RuntimeError("Hatchet extension application identity is unavailable")
        return self._application_id


def _validate_result(value: Any) -> Mapping[str, Any]:
    """Bound and detach untrusted provider output before durable persistence."""

    return normalize_json_mapping(value, "Hatchet continuation result")


async def _retry_pause(operation: str, attempt: int) -> None:
    """Back off provider retries while emitting sparse, payload-free diagnostics."""

    if attempt == 1 or attempt & (attempt - 1) == 0:
        _LOGGER.warning("Hatchet %s unavailable; retrying", operation)
    delay = min(
        _RETRY_INITIAL_SECONDS * (2 ** min(attempt - 1, 10)),
        _RETRY_MAX_SECONDS,
    )
    await asyncio.sleep(delay)


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
