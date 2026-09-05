"""Secret-safe adapter over the official Hatchet Python SDK."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
import os
from typing import Any, Protocol

from harnest.context import context

from .payloads import normalize_json_mapping


class HatchetExtensionError(RuntimeError):
    """A Hatchet call failed without retaining provider exception payloads."""


class HatchetRunStatus(str, Enum):
    """Provider statuses that affect continuation decisions."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class HatchetRun:
    """Portable identity for work owned by the external Hatchet runtime."""

    run_id: str
    workflow_name: str
    correlation_id: str

    def __post_init__(self) -> None:
        """Reject ambiguous provider identities before they reach continuation keys."""

        _require_text(self.run_id, "run id")
        _require_text(self.workflow_name, "workflow name")
        _require_text(self.correlation_id, "correlation id")


class HatchetTransport(Protocol):
    """Narrow external-runtime operations used by the extension policy layer."""

    async def run(
        self,
        workflow_name: str,
        job_input: Mapping[str, Any],
        *,
        correlation_id: str,
        idempotency_key: str | None = None,
    ) -> HatchetRun: ...

    async def status(self, job: HatchetRun) -> HatchetRunStatus: ...

    async def result(self, job: HatchetRun) -> Mapping[str, Any]: ...

    async def cancel(self, job: HatchetRun) -> None: ...

    async def aclose(self) -> None: ...


class HatchetSDKTransport:
    """Use public async SDK calls without starting an in-process worker."""

    def __init__(self, token: str) -> None:
        """Reveal the opaque token only at the SDK construction boundary."""

        _require_text(token, "credential token")
        client, failure = _create_sdk_client(token)
        if failure is not None:
            raise failure
        self._client = client

    async def run(
        self,
        workflow_name: str,
        job_input: Mapping[str, Any],
        *,
        correlation_id: str,
        idempotency_key: str | None = None,
    ) -> HatchetRun:
        """Submit replay-safe work while workers remain external to the plugin."""

        _require_text(workflow_name, "workflow name")
        _require_text(correlation_id, "correlation id")
        payload = normalize_json_mapping(job_input, "Hatchet job input")
        run_id = await self._submit(
            workflow_name,
            payload,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        _require_text(run_id, "provider run id")
        return HatchetRun(run_id, workflow_name, correlation_id)

    async def _submit(
        self,
        workflow_name: str,
        payload: Mapping[str, Any],
        *,
        correlation_id: str,
        idempotency_key: str | None,
    ) -> str:
        """Use Hatchet's native key so LangGraph replay cannot duplicate work."""

        if idempotency_key is None:
            details = await self._call(
                "create",
                self._runs().aio_create(
                    workflow_name,
                    payload,
                    additional_metadata=_metadata(correlation_id),
                ),
            )
            metadata = getattr(getattr(details, "run", None), "metadata", None)
            return getattr(metadata, "id", None)
        return await self._submit_idempotent(
            workflow_name,
            payload,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )

    async def _submit_idempotent(
        self,
        workflow_name: str,
        payload: Mapping[str, Any],
        *,
        correlation_id: str,
        idempotency_key: str,
    ) -> str:
        """Recover Hatchet's existing run ID after an idempotency collision."""

        try:
            from hatchet_sdk.exceptions import IdempotencyCollisionError
            from hatchet_sdk.types.trigger import TriggerWorkflowOptions
        except (ImportError, AttributeError) as error:
            raise HatchetExtensionError(
                f"Hatchet trigger support failed with {type(error).__name__}"
            ) from None

        try:
            workflow = self._client.stubs.workflow(
                name=workflow_name, input_validator=dict
            )
            reference = await workflow.aio_run(
                dict(payload),
                options=TriggerWorkflowOptions(
                    key=idempotency_key,
                    additional_metadata=_metadata(correlation_id),
                ),
                wait_for_result=False,
            )
            return reference.workflow_run_id
        except IdempotencyCollisionError as error:
            # The collision proves Hatchet already committed this exact replay
            # key, so adopting its public run identity is the safe outcome.
            return error.existing_run_external_id
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise HatchetExtensionError(
                f"Hatchet create failed with {type(error).__name__}"
            ) from None

    async def status(self, job: HatchetRun) -> HatchetRunStatus:
        """Map provider enums into the plugin's stable public status contract."""

        _require_run(job)
        provider_status = await self._call(
            "status", self._runs().aio_get_status(job.run_id)
        )
        value = getattr(provider_status, "value", provider_status)
        try:
            return HatchetRunStatus(value)
        except (TypeError, ValueError):
            failure = HatchetExtensionError("Hatchet returned an unsupported run status")
        raise failure

    async def result(self, job: HatchetRun) -> Mapping[str, Any]:
        """Copy the terminal output so SDK-owned models cannot escape the boundary."""

        _require_run(job)
        result = await self._call(
            "result", self._runs().aio_get_result(job.run_id)
        )
        return normalize_json_mapping(result, "Hatchet result")

    async def cancel(self, job: HatchetRun) -> None:
        """Request provider cancellation without stopping its worker or service."""

        _require_run(job)
        await self._call("cancel", self._runs().aio_cancel(job.run_id))

    async def aclose(self) -> None:
        """Release the SDK graph after its per-call REST contexts have closed."""

        # Hatchet SDK 1.x has no public client close method. This adapter avoids
        # its pooled result listener and uses REST calls that close per request,
        # so releasing the graph is the complete supported ownership boundary.
        self._client = None

    def _runs(self) -> Any:
        """Return the public runs client only while this adapter is active."""

        if self._client is None:
            raise HatchetExtensionError("Hatchet client is closed")
        return self._client.runs

    async def _call(self, operation: str, awaitable: Any) -> Any:
        """Detach provider failures because their messages can contain credentials."""

        failure: HatchetExtensionError | None = None
        try:
            return await awaitable
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failure = HatchetExtensionError(
                f"Hatchet {operation} failed with {type(error).__name__}"
            )
        # Raising after the provider handler exits prevents its exception object
        # from remaining reachable through Python's implicit context chain.
        raise failure


async def open_hatchet_transport(scopes: Sequence[str]) -> HatchetTransport:
    """Resolve an opaque invocation credential immediately before SDK creation."""

    credential = await context.credentials.resolve("hatchet", scopes)
    material = credential.reveal()
    if not isinstance(material, str) or not material:
        raise HatchetExtensionError("Hatchet credential must contain a token string")
    return HatchetSDKTransport(material)


async def open_hatchet_recovery_transport() -> HatchetTransport:
    """Create an application-owned reader before any invocation is active.

    Recovery deliberately uses the deployment service credential because an
    invocation credential context does not exist during extension startup. The
    value crosses only this SDK boundary and is never retained by Harnest state.
    """

    token = os.environ.get("HATCHET_CLIENT_TOKEN")
    if not isinstance(token, str) or not token:
        raise HatchetExtensionError(
            "Hatchet recovery requires an application service credential"
        )
    return HatchetSDKTransport(token)


def _create_sdk_client(
    token: str,
) -> tuple[Any | None, HatchetExtensionError | None]:
    """Construct the SDK while detaching potentially secret-bearing failures."""

    try:
        from hatchet_sdk import Hatchet
        from hatchet_sdk.config import ClientConfig

        # Explicitly disable SDK dotenv discovery. Deployment configuration may
        # still use reviewed HATCHET_CLIENT_* environment variables, but a
        # process working directory cannot silently contribute credentials.
        return Hatchet(config=ClientConfig(token=token, _env_file=None)), None
    except Exception as error:
        return None, HatchetExtensionError(
            f"Hatchet client initialization failed with {type(error).__name__}"
        )


def _metadata(correlation_id: str) -> dict[str, str]:
    """Build the provider-visible correlation labels without user payloads."""

    return {
        "harnest.correlation_id": correlation_id,
        "harnest.extension": "hatchet",
    }


def _require_run(job: Any) -> None:
    """Reject arbitrary objects before dispatching with their attributes."""

    if not isinstance(job, HatchetRun):
        raise TypeError("Hatchet job must be HatchetRun")


def _require_text(value: Any, label: str) -> None:
    """Keep provider and correlation identifiers explicit and non-empty."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Hatchet {label} must be a non-empty string")


__all__ = [
    "HatchetExtensionError",
    "HatchetRun",
    "HatchetRunStatus",
    "HatchetSDKTransport",
    "HatchetTransport",
    "open_hatchet_recovery_transport",
    "open_hatchet_transport",
]
