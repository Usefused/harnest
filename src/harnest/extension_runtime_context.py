"""Restricted application and invocation capabilities for runtime extensions."""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
import re
from types import MappingProxyType
from typing import Any, AsyncIterator, Iterator, Literal, Mapping, Sequence

from . import context
from .context import ContextResourceError
from .logging import get_logger


_STORAGE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._~-]{0,63}$")
_OPERATION = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_TRIGGERS = frozenset({"agent", "user"})
_AUDIT = get_logger("extension.audit")


@dataclass(frozen=True, slots=True)
class ExtensionStartContext:
    """Application facts available while one same-process extension starts.

    The context deliberately excludes invocation credentials and framework
    storage authorities. Extensions may reuse only explicitly named application
    storage whose lifecycle Harnest has already started.
    """

    extension_name: str
    framework: str
    root_agent_name: str
    _custom_stores: Mapping[str, Any] = field(repr=False)
    _continuations: Any | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Validate public identity and hide the copied storage registry."""

        _require_text(self.extension_name, "extension name")
        if self.framework not in {"adk", "langgraph"}:
            raise ValueError("extension framework must be adk or langgraph")
        _require_text(self.root_agent_name, "root agent name")
        if not isinstance(self._custom_stores, Mapping):
            raise TypeError("extension custom storage must be a mapping")
        object.__setattr__(
            self, "_custom_stores", MappingProxyType(dict(self._custom_stores))
        )

    def storage(
        self, name: str, expected_type: type[Any] | None = None
    ) -> Any:
        """Return one named custom store without exposing the registry."""

        _validate_storage_name(name)
        if name not in self._custom_stores:
            raise ContextResourceError(
                f"custom storage {name!r} is not available to this extension"
            )
        value = self._custom_stores[name]
        if expected_type is not None and not isinstance(value, expected_type):
            raise ContextResourceError(
                f"custom storage {name!r} must be {expected_type.__name__}; "
                f"got {type(value).__name__}"
            )
        return value

    @property
    def continuations(self) -> Any:
        """Return provider-bound completion authority when explicitly declared."""

        if self._continuations is None:
            raise ContextResourceError(
                "extension continuation authority was not declared"
            )
        return self._continuations


@dataclass(frozen=True, slots=True, repr=False)
class ExtensionInvocationBinding:
    """Pair an authored singleton with its invocation-specific context."""

    extension: Any = field(repr=False)
    extension_context: Any = field(repr=False)


_EMPTY_EXTENSION_BINDINGS: Mapping[str, ExtensionInvocationBinding] = MappingProxyType({})


class ExtensionContextAccess:
    """Resolve one extension context without making installed names enumerable."""

    def __call__(
        self, name: str, expected_type: type[Any] | None = None
    ) -> Any:
        """Return one invocation view and optionally validate its public type."""

        _require_text(name, "extension name")
        active = context.current()
        binding = active._extension_bindings.get(name)
        if binding is None:
            raise ContextResourceError(
                f"extension {name!r} is not available in this invocation"
            )
        value = binding.extension_context
        _require_extension_context(value)
        if expected_type is not None and not isinstance(value, expected_type):
            raise ContextResourceError(
                f"extension {name!r} must expose {expected_type.__name__}; "
                f"got {type(value).__name__}"
            )
        return value


extensions = ExtensionContextAccess()


@contextmanager
def activate_extension_bindings(
    bindings: Mapping[str, ExtensionInvocationBinding],
) -> Iterator[None]:
    """Bind singleton `.context` properties for only the current task."""

    tokens = bind_extension_bindings(bindings)
    try:
        yield
    finally:
        reset_extension_bindings(tokens)


def bind_extension_bindings(
    bindings: Mapping[str, ExtensionInvocationBinding],
) -> tuple[tuple[Any, Any], ...]:
    """Return task-owned tokens for framework adapters with split callbacks."""

    if not bindings:
        return ()
    from .extensions import Extension

    tokens: list[tuple[Any, Any]] = []
    try:
        for binding in bindings.values():
            # Binding is a Harnest ownership primitive, not an authored
            # extension point; bypass subclass overrides at this boundary.
            token = Extension._bind_context(binding.extension, binding.extension_context)
            tokens.append((binding.extension, token))
    except BaseException:
        for extension, token in reversed(tokens):
            Extension._reset_context(extension, token)
        raise
    return tuple(tokens)


def reset_extension_bindings(tokens: Sequence[tuple[Any, Any]]) -> None:
    """Reset tokens in the same task which opened the split activation."""

    if not tokens:
        return
    from .extensions import Extension

    for extension, token in reversed(tokens):
        Extension._reset_context(extension, token)


def revoke_extension_bindings(
    bindings: Mapping[str, ExtensionInvocationBinding],
) -> None:
    """Invalidate retained views, including bindings copied into child tasks."""

    if not bindings:
        return
    from .extensions import ExtensionContext

    for binding in bindings.values():
        ExtensionContext._revoke(binding.extension_context)


def validate_extension_bindings(
    bindings: Mapping[str, Any],
) -> Mapping[str, ExtensionInvocationBinding]:
    """Freeze only manager-created bindings at the AgentContext boundary."""

    if not isinstance(bindings, Mapping):
        raise TypeError("extension bindings must be a mapping")
    if not bindings:
        return _EMPTY_EXTENSION_BINDINGS
    normalized = dict(bindings)
    for name, binding in normalized.items():
        _require_text(name, "extension name")
        if not isinstance(binding, ExtensionInvocationBinding):
            raise TypeError(
                "extension bindings must contain ExtensionInvocationBinding values"
            )
        _require_extension_context(binding.extension_context)
        if binding.extension_context.extension_name != name:
            raise ValueError("extension binding name must match its context identity")
    return MappingProxyType(normalized)


@asynccontextmanager
async def extension_mutation(
    extension_name: str,
    operation: str,
    *,
    trigger: Literal["agent", "user"],
) -> AsyncIterator[None]:
    """Audit one exact durable operation after commit or on correlated failure."""

    _require_text(extension_name, "extension name")
    _validate_operation(operation)
    if trigger not in _TRIGGERS:
        raise ValueError("extension mutation trigger must be user or agent")
    try:
        yield
    except BaseException:
        _audit_mutation(extension_name, operation, trigger, "failed")
        raise
    _audit_mutation(extension_name, operation, trigger, "committed")


def _require_extension_context(value: Any) -> None:
    """Validate through the public base without importing it during bootstrap."""

    from .extensions import ExtensionContext

    if not isinstance(value, ExtensionContext):
        raise TypeError("extension invocation context must inherit ExtensionContext")
    # Revocation is host-owned; authored overrides cannot weaken the check.
    ExtensionContext._require_active(value)


def _audit_mutation(
    extension_name: str, operation: str, trigger: str, outcome: str
) -> None:
    """Emit stable dimensions only; OTEL supplies invocation correlation."""

    _AUDIT.info(
        "extension.mutation",
        operation=operation,
        trigger=trigger,
        outcome=outcome,
        extension=extension_name,
    )


def _validate_storage_name(name: Any) -> None:
    """Keep extension storage lookup aligned with lifecycle storage names."""

    if not isinstance(name, str) or not _STORAGE_NAME.fullmatch(name):
        raise ValueError("custom storage name must be a valid storage identifier")


def _validate_operation(operation: Any) -> None:
    """Bound audit dimensions so authored payloads cannot become operation names."""

    if not isinstance(operation, str) or not _OPERATION.fullmatch(operation):
        raise ValueError("extension mutation operation must be a stable identifier")


def _require_text(value: Any, label: str) -> None:
    """Validate identity text without coercing secret-bearing authored values."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")


__all__ = [
    "ExtensionContextAccess",
    "ExtensionInvocationBinding",
    "ExtensionStartContext",
    "extension_mutation",
    "extensions",
]
