"""Bounded asynchronous evaluation of application-owned decision providers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import inspect
import time
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from opentelemetry.trace import StatusCode

from .decision_policy import DecisionBinding, DecisionOutcome
from .decision_types import (
    DecisionCapabilities, DecisionDefinition, DecisionError, DecisionProvider, DecisionProviderError,
    DecisionRequest, DecisionResponse, DecisionTimeoutError, DecisionValidationError,
    finite, identifier,
)
from .decision_validation import validate_capabilities, validate_response
from .tracing import get_tracer


@dataclass(frozen=True, slots=True)
class DecisionEvaluation:
    """Validated results or an explicit failure outcome, with provider provenance."""

    decision: str
    version: str
    provider: str
    provider_version: str
    response: DecisionResponse | None = field(repr=False)
    outcome: DecisionOutcome | None
    duration_seconds: float
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _Provider:
    """Snapshot advertised guarantees independently of mutable client state."""

    client: DecisionProvider
    capabilities: DecisionCapabilities
    version: str


class Decisions:
    """Register providers and named decisions; lifecycle resources own client cleanup."""

    def __init__(
        self, *, providers: Mapping[str, DecisionProvider],
        bindings: Sequence[DecisionBinding], timeout_seconds: float = 10,
    ) -> None:
        """Validate every binding without evaluating a model or acquiring client resources."""
        finite(timeout_seconds, minimum=0)
        if timeout_seconds == 0:
            raise ValueError("decision timeout must be positive")
        self._timeout = timeout_seconds
        self._providers = MappingProxyType(_providers(providers))
        self._bindings = MappingProxyType(_bindings(bindings, self._providers))

    async def evaluate(self, name: str, state: Mapping[str, Any]) -> DecisionEvaluation:
        """Evaluate an isolated snapshot once; failures never trigger implicit retries."""
        identifier(name)
        binding = self._bindings.get(name)
        if binding is None:
            raise KeyError("decision is not registered")
        return await self._evaluate_binding(binding, state)

    async def evaluate_definition(
        self, definition: DecisionDefinition, state: Mapping[str, Any], *, provider: str | None = None,
    ) -> DecisionEvaluation:
        """Evaluate a runtime-built definition without mutating registered decisions."""
        if provider is None:
            if len(self._providers) != 1:
                raise ValueError("choose a provider when the registry does not contain exactly one")
            provider = next(iter(self._providers))
        binding = DecisionBinding(definition, provider)
        _bindings((binding,), self._providers)
        return await self._evaluate_binding(binding, state)

    async def _evaluate_binding(self, binding: DecisionBinding, state: Mapping[str, Any]) -> DecisionEvaluation:
        """Share validation, bounded execution and telemetry for static and dynamic definitions."""
        request = DecisionRequest(binding.definition, state)
        provider = self._providers[binding.provider]
        # Automatic exception recording would serialize provider errors containing
        # request payloads or credentials. Only fixed categories cross telemetry.
        with get_tracer("harnest.decisions").start_as_current_span(
            "harnest.decision.evaluate", attributes=_attributes(binding, provider),
            record_exception=False, set_status_on_exception=False,
        ) as span:
            return await self._evaluate(binding, request, provider, span)

    async def _evaluate(
        self, binding: DecisionBinding, request: DecisionRequest, provider: _Provider, span: Any,
    ) -> DecisionEvaluation:
        """Record safe outcomes while preserving cancellation and explicit failure policy."""
        started = time.monotonic()
        try:
            response, error = await _call(provider, request, self._timeout)
        except asyncio.CancelledError:
            span.set_attribute("harnest.decision.outcome", "cancelled")
            span.set_status(StatusCode.ERROR)
            raise
        duration = time.monotonic() - started
        category = _error_category(error)
        span.set_attribute("harnest.decision.duration_seconds", duration)
        span.set_attribute("harnest.decision.outcome", category or "success")
        if error is not None:
            span.set_status(StatusCode.ERROR)
            if binding.on_error is None:
                raise error
            outcome = binding.on_error
        else:
            outcome = binding.policy.apply(response) if binding.policy is not None else None
        if outcome is not None:
            span.set_attribute("harnest.decision.action", outcome.action.value)
        return DecisionEvaluation(
            binding.definition.name, binding.definition.version, binding.provider,
            provider.version, response, outcome, duration, category,
        )


def _providers(values: Mapping[str, DecisionProvider]) -> dict[str, _Provider]:
    """Validate custom provider contracts without constructing a vendor adapter."""
    if not isinstance(values, Mapping):
        raise TypeError("decision providers must be a mapping")
    result = {}
    for name, provider in values.items():
        identifier(name)
        if not isinstance(provider, DecisionProvider) or not inspect.iscoroutinefunction(provider.evaluate):
            raise TypeError("decision providers must implement asynchronous evaluate")
        if not isinstance(provider.capabilities, DecisionCapabilities):
            raise TypeError("provider capabilities must be DecisionCapabilities")
        identifier(provider.version)
        result[name] = _Provider(provider, provider.capabilities, provider.version)
    return result


def _bindings(values: Sequence[DecisionBinding], providers: Mapping[str, _Provider]) -> dict[str, DecisionBinding]:
    """Catch duplicate names, missing providers and incompatible policies at registration."""
    result = {}
    for binding in values:
        if not isinstance(binding, DecisionBinding):
            raise TypeError("decision bindings must contain DecisionBinding")
        if binding.definition.name in result:
            raise ValueError("decision names must be unique")
        if binding.provider not in providers:
            raise ValueError("decision binding references an unknown provider")
        capabilities = providers[binding.provider].capabilities
        validate_capabilities(binding.definition, capabilities)
        if binding.policy is not None:
            binding.policy.validate(binding.definition, capabilities)
        result[binding.definition.name] = binding
    return result


async def _call(
    provider: _Provider, request: DecisionRequest, timeout: float,
) -> tuple[DecisionResponse | None, DecisionError | None]:
    """Detach provider exceptions before returning or raising a public error."""
    try:
        response = await asyncio.wait_for(provider.client.evaluate(request), timeout)
    except asyncio.CancelledError:
        # Cancellation belongs to the caller; an on_error branch must not resume it.
        raise
    except asyncio.TimeoutError:
        return None, DecisionTimeoutError("decision evaluation timed out")
    except Exception:
        return None, DecisionProviderError("decision provider evaluation failed")
    try:
        validate_response(request.definition, response, provider.capabilities)
    except DecisionValidationError:
        return None, DecisionValidationError("decision provider returned an invalid response")
    return response, None


def _error_category(error: DecisionError | None) -> str | None:
    """Use a closed telemetry vocabulary without provider exception strings."""
    return {
        DecisionTimeoutError: "timeout", DecisionProviderError: "provider_error",
        DecisionValidationError: "invalid_response", type(None): None,
    }[type(error)]


def _attributes(binding: DecisionBinding, provider: _Provider) -> dict[str, str]:
    """Emit authored identities only, omitting state, questions, results and destinations."""
    return {
        "harnest.decision.name": binding.definition.name,
        "harnest.decision.version": binding.definition.version,
        "harnest.decision.provider": binding.provider,
        "harnest.decision.provider_version": provider.version,
    }


class DecisionContext:
    """A revocable decision facade bound to the invocation that obtained it."""

    def __init__(self, active: Any) -> None:
        """Capture an active context; resolve the explicitly published decisions resource."""
        self._active = active
        self._decisions = active.resource("decisions", Decisions)

    async def evaluate(self, name: str, state: Mapping[str, Any]) -> DecisionEvaluation:
        """Evaluate within the owning scope and record only explicitly public results."""
        self._require_scope()
        result = await self._decisions.evaluate(name, state)
        self._require_scope()
        self._active._decision_output.record(result, agent=self._active.agent_name)
        return result

    def _require_scope(self) -> None:
        """Recheck lifetime and identity before accepting work or returning a result."""
        from .context import ContextUnavailableError, current

        self._active._require_active()
        if current() is not self._active:
            raise ContextUnavailableError("decision access belongs to the invocation and agent that obtained it")
