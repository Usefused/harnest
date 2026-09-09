"""Shared synchronous/asynchronous execution of optional token policy."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import inspect
import logging
from typing import Any

from .token_reduction import estimate_tokens, reduce_request, selected_tools
from .tokens import (
    TokenBudgetExceeded, TokenCount, TokenPolicy, TokenPolicyError,
    TokenReport, TokenRequest, _BudgetState, _STATE,
)


@dataclass
class PreparedCall:
    """Carry content-free accounting alongside one detached provider request."""

    request: TokenRequest
    policy: TokenPolicy
    name: str
    calls: int
    before: TokenCount
    after: TokenCount
    exceeded: tuple[str, ...]
    messages_removed: int
    tools_removed: int
    failure: TokenBudgetExceeded | None = None

    def report(self, phase: str, **usage: Any) -> TokenReport:
        """Construct a payload-free observer event from validated counters."""
        return TokenReport(
            self.name, phase, self.calls, self.before, self.after, self.exceeded,
            self.messages_removed, self.tools_removed, **usage,
        )


def _steps(request: TokenRequest, policy: TokenPolicy):
    """Share strategy ordering across sync and async framework adapters."""
    counter = policy.count_tokens or estimate_tokens
    before = yield counter(deepcopy(request))
    current = reduce_request(request, policy)
    if policy.request_transform is not None:
        current = yield policy.request_transform(current)
        _validate_transform(request, current)
    if policy.tool_selector is not None:
        names = yield policy.tool_selector(deepcopy(current))
        current = selected_tools(current, names)
    # Counting can call a provider tokenizer; unchanged input needs one pass.
    after = before if current == request else (yield counter(deepcopy(current)))
    return current, _count(before), _count(after)


def _validate_transform(original: TokenRequest, current: Any) -> None:
    """Keep provider and tool authority outside arbitrary context transforms."""
    if not isinstance(current, TokenRequest):
        raise TokenPolicyError("request_transform must return TokenRequest")
    if (current.framework, current.model) != (original.framework, original.model):
        raise TokenPolicyError("request_transform cannot change framework or model")
    if current.context_metadata != original.context_metadata:
        raise TokenPolicyError("request_transform cannot change context_metadata")
    if current.tools != original.tools:
        raise TokenPolicyError("use tool_selector to select existing tools")
    if not isinstance(current.messages, tuple) or not all(
        isinstance(message, dict) for message in current.messages
    ):
        raise TokenPolicyError("request_transform messages must be a tuple of dictionaries")
    if not isinstance(current.settings, dict):
        raise TokenPolicyError("request_transform settings must be a dictionary")


def _count(value: Any) -> TokenCount:
    """Require explicit provenance instead of silently treating estimates as facts."""
    if not isinstance(value, TokenCount):
        raise TokenPolicyError("count_tokens must return TokenCount")
    return value


async def prepare(request: TokenRequest, policy: TokenPolicy, name: str) -> PreparedCall:
    """Await authored strategies, then atomically admit one model attempt."""
    steps = _steps(request, policy)
    value = None
    try:
        while True:
            try:
                pending = steps.send(value)
            except StopIteration as done:
                current, before, after = done.value
                break
            value = await pending if inspect.isawaitable(pending) else pending
    finally:
        steps.close()
    call = _admit(request, current, before, after, policy, name)
    await observe(call, "blocked" if call.failure else "before")
    if call.failure is not None:
        raise call.failure
    return call


def prepare_sync(request: TokenRequest, policy: TokenPolicy, name: str) -> PreparedCall:
    """Reject async strategies without starting an event loop inside model work."""
    steps = _steps(request, policy)
    value = None
    try:
        while True:
            try:
                value = _sync(steps.send(value))
            except StopIteration as done:
                current, before, after = done.value
                break
    finally:
        steps.close()
    call = _admit(request, current, before, after, policy, name)
    observe_sync(call, "blocked" if call.failure else "before")
    if call.failure is not None:
        raise call.failure
    return call


def _admit(
    original: TokenRequest, current: TokenRequest, before: TokenCount,
    after: TokenCount, policy: TokenPolicy, name: str,
) -> PreparedCall:
    """Check reduced input before reserving a call, preserving retry accounting."""
    violations = _violations(after.tokens, policy)
    state = _STATE.get()
    if state is None and policy.max_model_calls is not None:
        raise TokenPolicyError("max_model_calls requires a Harnest invocation or token_policy_scope")
    calls, failure = _reserve(state or _BudgetState(), name, policy, violations)
    if policy.max_model_calls is not None and calls > policy.max_model_calls:
        violations.append(("model_calls", policy.max_model_calls, calls))
    return PreparedCall(
        current, policy, name, calls, before, after,
        tuple(item[0] for item in violations),
        max(0, len(original.messages) - len(current.messages)),
        len(original.tools) - len(current.tools), failure,
    )


def _reserve(
    state: _BudgetState, name: str, policy: TokenPolicy,
    violations: list[tuple[str, int, int]],
) -> tuple[int, TokenBudgetExceeded | None]:
    """Report rejected attempts without consuming a dispatch reservation."""
    if policy.enforce and violations:
        return state.calls.get(name, 0), TokenBudgetExceeded(*violations[0])
    try:
        return state.reserve(name, policy), None
    except TokenBudgetExceeded as error:
        return error.observed, error


def _violations(tokens: int, policy: TokenPolicy) -> list[tuple[str, int, int]]:
    """Reserve response capacity independently of the input-only budget."""
    reserve = max(policy.reserve_output_tokens, policy.max_output_tokens or 0)
    checks = (
        ("input_tokens", policy.max_input_tokens, tokens),
        ("context_window", policy.context_window, tokens + reserve),
    )
    return [(name, limit, value) for name, limit, value in checks if limit is not None and value > limit]


def output_settings(settings: dict[str, Any], policy: TokenPolicy, key: str) -> dict[str, Any]:
    """Never increase an existing native output limit while enforcing a ceiling."""
    result = dict(settings)
    if policy.enforce and policy.max_output_tokens is not None:
        existing = result.get(key)
        limit = policy.max_output_tokens
        result[key] = min(existing, limit) if type(existing) is int else limit
    return result


def _sync(value: Any) -> Any:
    """Close unused coroutine objects before reporting an unsupported sync hook."""
    if not inspect.isawaitable(value):
        return value
    close = getattr(value, "close", None)
    if callable(close):
        close()
    raise TokenPolicyError("async token strategies require async model execution")


async def observe(call: PreparedCall, phase: str, **usage: Any) -> None:
    """Keep observer failures from interrupting otherwise valid model execution."""
    if not call.policy.observe:
        return
    try:
        value = _notify(call, phase, usage)
        if inspect.isawaitable(value):
            await value
    except Exception:
        logging.getLogger("harnest.tokens").warning("token observer failed")


def observe_sync(call: PreparedCall, phase: str, **usage: Any) -> None:
    """Use the same content-free observations for synchronous model execution."""
    if not call.policy.observe:
        return
    try:
        _sync(_notify(call, phase, usage))
    except Exception:
        logging.getLogger("harnest.tokens").warning("token observer failed")


def _notify(call: PreparedCall, phase: str, usage: dict[str, Any]) -> Any:
    """Use an authored observer or numeric diagnostics without prompt logging."""
    report = call.report(phase, **usage)
    if call.policy.observer is not None:
        return call.policy.observer(report)
    logging.getLogger("harnest.tokens").info("token policy: %s", report)
    return None
