"""Opt-in model context policies, budgets, and trusted invocation overrides."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from typing import Any


class TokenPolicyError(RuntimeError):
    """A configured token strategy cannot be applied to a model request."""


class TokenBudgetExceeded(TokenPolicyError):
    """A model call was stopped before dispatch by an enabled budget."""

    def __init__(self, budget: str, limit: int, observed: int) -> None:
        """Expose numeric diagnostics without including model content."""
        self.budget, self.limit, self.observed = budget, limit, observed
        super().__init__(f"token policy {budget} budget exceeded: {observed} > {limit}")


@dataclass(frozen=True, slots=True)
class TokenCount:
    """A count with explicit provenance; estimates are never provider usage."""

    tokens: int
    estimated: bool = True

    def __post_init__(self) -> None:
        """Reject malformed counter results before budget comparisons."""
        _integer(self.tokens, "tokens", minimum=0)
        if type(self.estimated) is not bool:
            raise TypeError("estimated must be boolean")


@dataclass(frozen=True, slots=True)
class TokenRequest:
    """Detached model input for strategies; message shapes follow framework.

    ADK messages are Content dictionaries; LangGraph messages use LangChain's
    messages_to_dict representation. Settings are native generation options.
    Tool descriptors are informational; select existing tools by name instead
    of manufacturing new callable capabilities.
    """

    framework: str
    model: str
    messages: tuple[dict[str, Any], ...]
    system: Any = None
    tools: tuple[dict[str, Any], ...] = ()
    settings: dict[str, Any] = field(default_factory=dict)
    context_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TokenReport:
    """Content-free observations for one model call or rejected attempt."""

    agent_name: str
    phase: str
    model_calls: int
    input_before: TokenCount | None = None
    input_after: TokenCount | None = None
    exceeded: tuple[str, ...] = ()
    messages_removed: int = 0
    tools_removed: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class TokenPolicy:
    """Independently enable observation, enforcement, and authored reductions.

    Merely constructing a policy observes; limits require enforce=True.
    Reductions run only when explicitly configured. Callbacks may be sync or
    async, but synchronous model execution requires synchronous callbacks.
    """

    enabled: bool = True
    observe: bool = True
    enforce: bool = False
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    output_token_parameter: str | None = None
    max_model_calls: int | None = None
    context_window: int | None = None
    reserve_output_tokens: int = 0
    keep_recent_turns: int | None = None
    max_tool_result_chars: int | None = None
    request_transform: Callable[[TokenRequest], Any] | None = field(default=None, repr=False)
    tool_selector: Callable[[TokenRequest], Any] | None = field(default=None, repr=False)
    count_tokens: Callable[[TokenRequest], Any] | None = field(default=None, repr=False)
    observer: Callable[[TokenReport], Any] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Validate independent controls without loading a tokenizer or model."""
        for name in ("enabled", "observe", "enforce"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be boolean")
        for name in (
            "max_input_tokens", "max_output_tokens", "max_model_calls",
            "context_window", "keep_recent_turns", "max_tool_result_chars",
        ):
            value = getattr(self, name)
            if value is not None:
                _integer(value, name, minimum=1)
        _integer(self.reserve_output_tokens, "reserve_output_tokens", minimum=0)
        if self.output_token_parameter not in {None, "max_tokens", "max_completion_tokens", "max_output_tokens"}:
            raise ValueError("output_token_parameter must name a supported output token parameter")
        _validate_callbacks(self)


def _integer(value: Any, name: str, *, minimum: int) -> None:
    """Exclude booleans and fractional values from capacity contracts."""
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def _validate_callbacks(policy: TokenPolicy) -> None:
    """Keep strategy errors at authoring time rather than model dispatch."""
    for name in ("request_transform", "tool_selector", "count_tokens", "observer"):
        value = getattr(policy, name)
        if value is not None and not callable(value):
            raise TypeError(f"{name} must be callable")


@dataclass
class _BudgetState:
    """Share atomic call reservations across tasks in one invocation."""

    calls: dict[str, int] = field(default_factory=dict)
    lock: Any = field(default_factory=Lock)

    def reserve(self, name: str, policy: TokenPolicy) -> int:
        """Count dispatched attempts, including failures, without oversubscription."""
        with self.lock:
            count = self.calls.get(name, 0) + 1
            limit = policy.max_model_calls
            if policy.enforce and limit is not None and count > limit:
                raise TokenBudgetExceeded("model_calls", limit, count)
            self.calls[name] = count
            return count


_OVERRIDES: ContextVar[Mapping[str | None, TokenPolicy | None]] = ContextVar(
    "harnest_token_overrides", default={}
)
_STATE: ContextVar[_BudgetState | None] = ContextVar("harnest_token_state", default=None)


@contextmanager
def token_policy_scope(
    policy: TokenPolicy | None, *, agent_name: str | None = None
) -> Iterator[None]:
    """Replace or disable policy for trusted code in this scope and child tasks.

    None disables policy. An agent-specific override takes precedence over a
    scope-wide override. HTTP metadata never activates this trusted control.
    Native agents need a configured adapter (TokenPolicy(enabled=False) is
    sufficient) before a scope can enable policy for them.
    """
    if policy is not None and not isinstance(policy, TokenPolicy):
        raise TypeError("policy must be TokenPolicy or None")
    if agent_name is not None and (not isinstance(agent_name, str) or not agent_name):
        raise ValueError("agent_name must be a non-empty string")
    token = _OVERRIDES.set({**_OVERRIDES.get(), agent_name: policy})
    state_token = _STATE.set(_STATE.get() or _BudgetState())
    try:
        yield
    finally:
        _STATE.reset(state_token)
        _OVERRIDES.reset(token)


@contextmanager
def _invocation_token_state(context: Any) -> Iterator[None]:
    """Reuse one state across stream advancements without leaking it to callers."""
    attributes = getattr(context, "attributes", {})
    state = attributes.setdefault("_harnest_token_budget", _BudgetState())
    token = _STATE.set(state)
    try:
        yield
    finally:
        _STATE.reset(token)


def _policy_for(default: TokenPolicy, name: str) -> TokenPolicy | None:
    """Resolve explicit overrides without treating disabled values as missing."""
    overrides = _OVERRIDES.get()
    policy = overrides.get(name, overrides.get(None, default))
    return policy if policy is not None and policy.enabled else None


__all__ = [
    "TokenBudgetExceeded", "TokenCount", "TokenPolicy", "TokenPolicyError",
    "TokenReport", "TokenRequest", "token_policy_scope",
]
