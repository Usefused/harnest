"""Restricted application and invocation capabilities for runtime plugins."""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
import re
from types import MappingProxyType
from typing import Any, AsyncIterator, Iterator, Literal, Mapping, Sequence

from .context import ContextResourceError, context
from .logging import get_logger


_STORAGE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._~-]{0,63}$")
_OPERATION = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_TRIGGERS = frozenset({"agent", "user"})
_AUDIT = get_logger("plugin.audit")


@dataclass(frozen=True, slots=True)
class PluginStartContext:
    """Application facts available while one same-process plugin starts.

    The context deliberately excludes invocation credentials and framework
    storage authorities. Plugins may reuse only explicitly named application
    storage whose lifecycle Harnest has already started.
    """

    plugin_name: str
    framework: str
    root_agent_name: str
    _custom_stores: Mapping[str, Any] = field(repr=False)
    _continuations: Any | None = field(default=None, repr=False)

    @property
    def extension_name(self) -> str:
        """Expose canonical extension identity while preserving legacy callers."""

        return self.plugin_name

    def __post_init__(self) -> None:
        """Validate public identity and hide the copied storage registry."""

        _require_text(self.plugin_name, "plugin name")
        if self.framework not in {"adk", "langgraph"}:
            raise ValueError("plugin framework must be adk or langgraph")
        _require_text(self.root_agent_name, "root agent name")
        if not isinstance(self._custom_stores, Mapping):
            raise TypeError("plugin custom storage must be a mapping")
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
                f"custom storage {name!r} is not available to this plugin"
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
                "plugin continuation authority was not declared"
            )
        return self._continuations


@dataclass(frozen=True, slots=True, repr=False)
class PluginInvocationBinding:
    """Pair an authored singleton with its invocation-specific context."""

    plugin: Any = field(repr=False)
    plugin_context: Any = field(repr=False)


_EMPTY_PLUGIN_BINDINGS: Mapping[str, PluginInvocationBinding] = MappingProxyType({})


class PluginContextAccess:
    """Resolve one plugin context without making installed names enumerable."""

    def __call__(
        self, name: str, expected_type: type[Any] | None = None
    ) -> Any:
        """Return one invocation view and optionally validate its public type."""

        _require_text(name, "plugin name")
        active = context.current()
        binding = active._plugin_bindings.get(name)
        if binding is None:
            raise ContextResourceError(
                f"plugin {name!r} is not available in this invocation"
            )
        value = binding.plugin_context
        _require_plugin_context(value)
        if expected_type is not None and not isinstance(value, expected_type):
            raise ContextResourceError(
                f"plugin {name!r} must expose {expected_type.__name__}; "
                f"got {type(value).__name__}"
            )
        return value


plugins = PluginContextAccess()


@contextmanager
def activate_plugin_bindings(
    bindings: Mapping[str, PluginInvocationBinding],
) -> Iterator[None]:
    """Bind singleton `.context` properties for only the current task."""

    tokens = bind_plugin_bindings(bindings)
    try:
        yield
    finally:
        reset_plugin_bindings(tokens)


def bind_plugin_bindings(
    bindings: Mapping[str, PluginInvocationBinding],
) -> tuple[tuple[Any, Any], ...]:
    """Return task-owned tokens for framework adapters with split callbacks."""

    if not bindings:
        return ()
    from .plugins import Plugin

    tokens: list[tuple[Any, Any]] = []
    try:
        for binding in bindings.values():
            # Binding is a Harnest ownership primitive, not an authored
            # extension point; bypass subclass overrides at this boundary.
            token = Plugin._bind_context(binding.plugin, binding.plugin_context)
            tokens.append((binding.plugin, token))
    except BaseException:
        for plugin, token in reversed(tokens):
            Plugin._reset_context(plugin, token)
        raise
    return tuple(tokens)


def reset_plugin_bindings(tokens: Sequence[tuple[Any, Any]]) -> None:
    """Reset tokens in the same task which opened the split activation."""

    if not tokens:
        return
    from .plugins import Plugin

    for plugin, token in reversed(tokens):
        Plugin._reset_context(plugin, token)


def revoke_plugin_bindings(
    bindings: Mapping[str, PluginInvocationBinding],
) -> None:
    """Invalidate retained views, including bindings copied into child tasks."""

    if not bindings:
        return
    from .plugins import PluginContext

    for binding in bindings.values():
        PluginContext._revoke(binding.plugin_context)


def validate_plugin_bindings(
    bindings: Mapping[str, Any],
) -> Mapping[str, PluginInvocationBinding]:
    """Freeze only manager-created bindings at the AgentContext boundary."""

    if not isinstance(bindings, Mapping):
        raise TypeError("plugin bindings must be a mapping")
    if not bindings:
        return _EMPTY_PLUGIN_BINDINGS
    normalized = dict(bindings)
    for name, binding in normalized.items():
        _require_text(name, "plugin name")
        if not isinstance(binding, PluginInvocationBinding):
            raise TypeError(
                "plugin bindings must contain PluginInvocationBinding values"
            )
        _require_plugin_context(binding.plugin_context)
        if binding.plugin_context.plugin_name != name:
            raise ValueError("plugin binding name must match its context identity")
    return MappingProxyType(normalized)


@asynccontextmanager
async def plugin_mutation(
    plugin_name: str,
    operation: str,
    *,
    trigger: Literal["agent", "user"],
) -> AsyncIterator[None]:
    """Audit one exact durable operation after commit or on correlated failure."""

    _require_text(plugin_name, "plugin name")
    _validate_operation(operation)
    if trigger not in _TRIGGERS:
        raise ValueError("plugin mutation trigger must be user or agent")
    try:
        yield
    except BaseException:
        _audit_mutation(plugin_name, operation, trigger, "failed")
        raise
    _audit_mutation(plugin_name, operation, trigger, "committed")


def _require_plugin_context(value: Any) -> None:
    """Validate through the public base without importing it during bootstrap."""

    from .plugins import PluginContext

    if not isinstance(value, PluginContext):
        raise TypeError("plugin invocation context must inherit PluginContext")
    # Revocation is host-owned; authored overrides cannot weaken the check.
    PluginContext._require_active(value)


def _audit_mutation(
    plugin_name: str, operation: str, trigger: str, outcome: str
) -> None:
    """Emit stable dimensions only; OTEL supplies invocation correlation."""

    _AUDIT.info(
        "plugin.mutation",
        operation=operation,
        trigger=trigger,
        outcome=outcome,
        plugin=plugin_name,
    )


def _validate_storage_name(name: Any) -> None:
    """Keep plugin storage lookup aligned with lifecycle storage names."""

    if not isinstance(name, str) or not _STORAGE_NAME.fullmatch(name):
        raise ValueError("custom storage name must be a valid storage identifier")


def _validate_operation(operation: Any) -> None:
    """Bound audit dimensions so authored payloads cannot become operation names."""

    if not isinstance(operation, str) or not _OPERATION.fullmatch(operation):
        raise ValueError("plugin mutation operation must be a stable identifier")


def _require_text(value: Any, label: str) -> None:
    """Validate identity text without coercing secret-bearing authored values."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")


__all__ = [
    "PluginContextAccess",
    "PluginInvocationBinding",
    "PluginStartContext",
    "plugin_mutation",
    "plugins",
]
