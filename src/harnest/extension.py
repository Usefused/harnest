"""Lifecycle extensions for Harnest agents.

Extensions add behavior around an invocation. They deliberately do not bundle
tools, agents, MCP clients, or skills. Plugins are the separate convention for
combining MCP clients with the skills that teach an agent how to use them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, MutableMapping


_EXTENSION_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FRAMEWORKS = frozenset({"adk", "langgraph"})


class _DropEvent:
    def __repr__(self) -> str:
        return "DROP_EVENT"


DROP_EVENT = _DropEvent()
"""Return this from ``on_event`` to omit an event before it is emitted."""

# Portable hooks may be synchronous or asynchronous. For transforming hooks,
# None means "keep the supplied value" and any other value replaces it.
TransformResult = object | Awaitable[object] | None
NotificationResult = Awaitable[None] | None
BeforeInvokeHook = Callable[["LifecycleContext", Any], TransformResult]
AfterInvokeHook = Callable[["LifecycleContext", Any], TransformResult]
EventHook = Callable[["LifecycleContext", Any], TransformResult]
ErrorHook = Callable[["LifecycleContext", BaseException], NotificationResult]


@dataclass(slots=True)
class LifecycleContext:
    """Portable context shared by all hooks for one invocation.

    ``attributes`` is an invocation-scoped scratchpad. An extension can, for
    example, record timing data before invocation and consume it afterward
    without putting framework-native state into the portable contract.
    """

    framework: str
    agent_name: str
    invocation_id: str
    user_id: str
    session_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    attributes: MutableMapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.framework not in _FRAMEWORKS:
            raise ValueError(
                f"unsupported extension framework {self.framework!r}; "
                "expected 'adk' or 'langgraph'"
            )
        for field_name in (
            "agent_name",
            "invocation_id",
            "user_id",
            "session_id",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        if not isinstance(self.attributes, MutableMapping):
            raise TypeError("attributes must be a mutable mapping")


@dataclass(frozen=True, slots=True)
class Extension:
    """A named set of framework-neutral invocation hooks.

    Portable hooks run in extension discovery order. ``before_invoke``,
    ``after_invoke``, and ``on_event`` return a replacement value or ``None``
    to leave it unchanged. Raising aborts the pipeline. ``on_error`` is a
    notification hook: its return value is ignored and the error remains an
    error.
    """

    name: str
    before_invoke: BeforeInvokeHook | None = None
    after_invoke: AfterInvokeHook | None = None
    on_event: EventHook | None = None
    on_error: ErrorHook | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _EXTENSION_NAME_PATTERN.fullmatch(
            self.name
        ):
            raise ValueError(
                "extension name must start with a letter or underscore and contain "
                "only ASCII letters, numbers, and underscores"
            )
        for hook_name in (
            "before_invoke",
            "after_invoke",
            "on_event",
            "on_error",
        ):
            hook = getattr(self, hook_name)
            if hook is not None and not callable(hook):
                raise TypeError(f"extension {hook_name} must be callable or None")

__all__ = [
    "AfterInvokeHook",
    "BeforeInvokeHook",
    "DROP_EVENT",
    "ErrorHook",
    "EventHook",
    "Extension",
    "LifecycleContext",
    "NotificationResult",
    "TransformResult",
]
