"""Provider-neutral continuation ports consumed by the Hatchet adapter."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Callable, Protocol


class SuspendedContinuation(Protocol):
    """Invocation-owned handle that resumes the suspended authored coroutine."""

    async def result(self) -> Mapping[str, Any]: ...


class InvocationContinuationPort(Protocol):
    """Provider-bound suspension authority available only during invocation."""

    async def suspend(
        self,
        external_id: str,
        *,
        capability: str,
        schema_id: str,
        validate: Callable[[Any], Mapping[str, Any]],
    ) -> SuspendedContinuation: ...


class PendingContinuation(Protocol):
    """Bounded recovery metadata without user prompts, results, or credentials."""

    external_id: str
    record: "PendingContinuationRecord"


class PendingContinuationRecord(Protocol):
    """Private identifiers needed to page and correlate provider recovery."""

    continuation_id: str
    run_id: str


class ContinuationProviderPort(Protocol):
    """Application authority for one plugin's external completions."""

    def register_schema(
        self, schema_id: str, validate: Callable[[Any], Mapping[str, Any]]
    ) -> None: ...

    async def complete(
        self, external_id: str, result: Mapping[str, Any]
    ) -> None: ...

    async def fail(self, external_id: str, error_code: str) -> None: ...

    async def list_pending(
        self, *, after: str | None = None, limit: int = 100
    ) -> Sequence[PendingContinuation]: ...


def provider_continuations(start_context: Any) -> ContinuationProviderPort:
    """Resolve the provider-bound application port from plugin startup context."""

    value = getattr(start_context, "continuations", None)
    if value is None:
        raise RuntimeError("Harnest external continuations are unavailable")
    return value


def invocation_continuations(plugin_context: Any) -> InvocationContinuationPort:
    """Resolve the provider-bound invocation port without retaining context state."""

    return plugin_context.continuations


__all__ = [
    "ContinuationProviderPort",
    "InvocationContinuationPort",
    "PendingContinuation",
    "PendingContinuationRecord",
    "SuspendedContinuation",
]
