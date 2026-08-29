"""Base APIs and controlled namespaces for authored runtime plugins."""

from __future__ import annotations

import importlib.util
import inspect
import keyword
import sys
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from threading import RLock
from types import ModuleType
from typing import Generic, Iterator, Sequence, TypeVar, cast

from harnest.plugin_runtime_context import PluginStartContext, plugin_mutation
from harnest.runtime_plugins import (
    RuntimePluginDescriptor,
    verify_runtime_plugin,
)


class PluginContextUnavailableError(RuntimeError):
    """A runtime plugin context was used outside its managed invocation."""


class PluginNamespaceError(RuntimeError):
    """A runtime-plugin namespace cannot be activated or safely released."""


class PluginImportError(RuntimeError):
    """An authored runtime-plugin module violates its export contract."""


@dataclass(slots=True)
class _PluginContextLifetime:
    """Share revocation with child tasks that copied task-local plugin state."""

    active: bool = True


class PluginContext:
    """Revocable invocation base that plugin-specific contexts may extend."""

    __slots__ = ("_continuations", "_lifetime", "_plugin_name")

    def __init__(self, plugin_name: str) -> None:
        """Bind the context to one identifier-safe runtime plugin."""

        if (
            not isinstance(plugin_name, str)
            or not plugin_name.isidentifier()
            or keyword.iskeyword(plugin_name)
        ):
            raise ValueError(
                "plugin context name must be a non-keyword Python identifier"
            )
        self._plugin_name = plugin_name
        self._lifetime = _PluginContextLifetime()
        self._continuations = None

    @property
    def plugin_name(self) -> str:
        """Return the plugin whose invocation authority this context carries."""

        return self._plugin_name

    @property
    def active(self) -> bool:
        """Report whether the owning managed invocation is still active."""

        return self._lifetime.active

    @property
    def continuations(self):
        """Return provider-bound suspension authority for this invocation."""

        self._require_active()
        if self._continuations is None:
            raise PluginContextUnavailableError(
                f"runtime plugin {self._plugin_name!r} did not declare "
                "context.continuations"
            )
        return self._continuations

    def _bind_continuations(self, value: object) -> None:
        """Install only the host-created port after authored context adaptation."""

        self._require_active()
        if self._continuations is not None:
            raise PluginNamespaceError("plugin continuation authority is already bound")
        self._continuations = value

    def _require_active(self) -> None:
        """Prevent retained child tasks from using authority after invocation exit."""

        if not self._lifetime.active:
            raise PluginContextUnavailableError(
                f"runtime plugin {self._plugin_name!r} context is no longer active"
            )

    def _revoke(self) -> None:
        """Revoke this context and every task that inherited the same object."""

        self._lifetime.active = False


ContextT = TypeVar("ContextT", bound=PluginContext)


class Plugin(Generic[ContextT]):
    """Application-owned runtime plugin with task-local invocation context."""

    __slots__ = ("_harnest_context", "_harnest_name")

    def __new__(cls, *args: object, **kwargs: object) -> "Plugin[ContextT]":
        """Install private state even when a subclass omits ``super().__init__``."""

        instance = cast("Plugin[ContextT]", super().__new__(cls))
        object.__setattr__(
            instance,
            "_harnest_context",
            ContextVar(
                f"harnest_plugin_context_{cls.__module__}_{cls.__qualname__}",
                default=None,
            ),
        )
        object.__setattr__(instance, "_harnest_name", None)
        return instance

    async def start(self, context: "PluginStartContext") -> None:
        """Initialize application-scoped resources after dependencies start."""

        return None

    async def stop(self) -> None:
        """Release application-scoped resources before dependencies stop."""

        return None

    def create_context(self, context: PluginContext) -> ContextT:
        """Adapt the managed invocation base to a plugin-specific context."""

        if not isinstance(context, PluginContext):
            raise TypeError("runtime plugin context must extend PluginContext")
        self._require_identity(context.plugin_name)
        return cast(ContextT, context)

    @property
    def context(self) -> ContextT:
        """Return the task-local plugin context for the active invocation."""

        active = self._harnest_context.get()
        if active is None:
            name = self._harnest_name or type(self).__name__
            raise PluginContextUnavailableError(
                f"runtime plugin {name!r} context is available only during an invocation"
            )
        # Calling the base method directly keeps an authored context subclass
        # from weakening host-owned revocation policy.
        PluginContext._require_active(active)
        return cast(ContextT, active)

    def _bind_context(self, context: PluginContext) -> Token[PluginContext | None]:
        """Bind one manager-created context to the current async execution."""

        if not isinstance(context, PluginContext):
            raise TypeError("runtime plugin context must extend PluginContext")
        Plugin._require_identity(self, context.plugin_name)
        PluginContext._require_active(context)
        return self._harnest_context.set(context)

    def _reset_context(self, token: Token[PluginContext | None]) -> None:
        """Restore the previous task-local context after invocation teardown."""

        self._harnest_context.reset(token)

    def _bind_identity(self, name: str) -> None:
        """Bind the authored singleton to its descriptor-owned public name."""

        if self._harnest_name not in {None, name}:
            raise PluginNamespaceError(
                f"runtime plugin instance is already bound as {self._harnest_name!r}"
            )
        self._harnest_name = name

    def _clear_identity(self, name: str) -> None:
        """Release only the descriptor identity owned by this activation."""

        if self._harnest_name == name:
            self._harnest_name = None

    def _require_identity(self, name: str) -> None:
        """Keep one plugin singleton from crossing descriptor boundaries."""

        if self._harnest_name is None:
            raise PluginNamespaceError("runtime plugin instance is not activated")
        if self._harnest_name != name:
            raise PluginNamespaceError(
                f"runtime plugin {self._harnest_name!r} cannot bind context for {name!r}"
            )


@dataclass(frozen=True, slots=True)
class ActivatedPlugin:
    """One validated namespace module and its authored singleton."""

    descriptor: RuntimePluginDescriptor
    module: ModuleType
    plugin: Plugin[PluginContext]


@dataclass(slots=True)
class _ActivationState:
    """Reference-count one process-wide plugin namespace transaction."""

    key: tuple[tuple[str, str, str], ...]
    plugins: tuple[ActivatedPlugin, ...]
    references: int = 1


_activation_lock = RLock()
_active_state: _ActivationState | None = None


def activate_runtime_plugins(
    descriptors: Sequence[RuntimePluginDescriptor],
) -> tuple[ActivatedPlugin, ...]:
    """Activate a dependency-ordered plugin set as one namespace transaction."""

    resolved = tuple(descriptors)
    if not resolved:
        return ()
    _validate_activation_order(resolved)
    for descriptor in resolved:
        verify_runtime_plugin(descriptor)
    key = _activation_key(resolved)
    global _active_state
    with _activation_lock:
        if _active_state is not None:
            if _active_state.key != key:
                raise PluginNamespaceError(
                    "harnest.plugins is already bound to another compiled agent"
                )
            _active_state.references += 1
            return _active_state.plugins
        activated: list[ActivatedPlugin] = []
        try:
            for descriptor in resolved:
                activated.append(_activate_plugin(descriptor))
        except Exception:
            _release_activated(tuple(reversed(activated)))
            raise
        _active_state = _ActivationState(key, tuple(activated))
        return _active_state.plugins


def release_runtime_plugins(
    descriptors: Sequence[RuntimePluginDescriptor],
) -> None:
    """Release one acquisition without disturbing a competing plugin set."""

    resolved = tuple(descriptors)
    if not resolved:
        return
    key = _activation_key(resolved)
    global _active_state
    with _activation_lock:
        if _active_state is None:
            return
        if _active_state.key != key:
            raise PluginNamespaceError(
                "cannot release runtime plugins owned by another compiled agent"
            )
        _active_state.references -= 1
        if _active_state.references > 0:
            return
        _release_activated(tuple(reversed(_active_state.plugins)))
        _active_state = None


@contextmanager
def runtime_plugin_namespaces(
    descriptors: Sequence[RuntimePluginDescriptor],
) -> Iterator[tuple[ActivatedPlugin, ...]]:
    """Activate runtime plugins for one bounded compiler or test operation."""

    resolved = tuple(descriptors)
    activated = activate_runtime_plugins(resolved)
    try:
        yield activated
    finally:
        if activated:
            release_runtime_plugins(resolved)


def _validate_activation_order(
    descriptors: Sequence[RuntimePluginDescriptor],
) -> None:
    """Require dependency-first descriptors before executing any authored code."""

    seen: set[str] = set()
    casefold_names: set[str] = set()
    for descriptor in descriptors:
        if not isinstance(descriptor, RuntimePluginDescriptor):
            raise TypeError("runtime plugin activation requires discovered descriptors")
        if descriptor.name in seen or descriptor.name.casefold() in casefold_names:
            raise PluginNamespaceError(
                f"duplicate runtime plugin activation name: {descriptor.name!r}"
            )
        unavailable = sorted(set(descriptor.requires) - seen)
        if unavailable:
            raise PluginNamespaceError(
                f"runtime plugin {descriptor.name!r} must follow dependencies: "
                + ", ".join(unavailable)
            )
        seen.add(descriptor.name)
        casefold_names.add(descriptor.name.casefold())


def _activation_key(
    descriptors: Sequence[RuntimePluginDescriptor],
) -> tuple[tuple[str, str, str], ...]:
    """Identify an immutable plugin set without retaining executable objects."""

    return tuple(
        (item.name, str(item.directory.resolve()), item.digest) for item in descriptors
    )


def _activate_plugin(descriptor: RuntimePluginDescriptor) -> ActivatedPlugin:
    """Load one plugin.py directly into its controlled public namespace."""

    namespace = descriptor.namespace
    parent = sys.modules[__name__]
    if namespace in sys.modules or hasattr(parent, descriptor.name):
        raise PluginNamespaceError(
            f"runtime plugin namespace is already occupied: {namespace}"
        )
    module = _plugin_module(descriptor)
    sys.modules[namespace] = module
    setattr(parent, descriptor.name, module)
    failure: str | None = None
    try:
        loader = module.__spec__.loader if module.__spec__ is not None else None
        if loader is None:
            raise ImportError("plugin module loader is unavailable")
        loader.exec_module(module)
    except Exception as error:
        failure = type(error).__name__
    if failure is not None:
        _remove_plugin_modules(descriptor.name)
        raise PluginImportError(
            f"failed to import runtime plugin {descriptor.name!r} with {failure}"
        )
    try:
        plugin = _validated_plugin_export(module, descriptor)
    except Exception:
        _remove_plugin_modules(descriptor.name)
        raise
    Plugin._bind_identity(plugin, descriptor.name)
    return ActivatedPlugin(descriptor, module, plugin)


def _plugin_module(descriptor: RuntimePluginDescriptor) -> ModuleType:
    """Create a package-like module so relative plugin imports stay contained."""

    spec = importlib.util.spec_from_file_location(
        descriptor.namespace,
        descriptor.source,
        submodule_search_locations=[str(descriptor.directory)],
    )
    if spec is None or spec.loader is None:
        raise PluginImportError(
            f"cannot create a module for runtime plugin {descriptor.name!r}"
        )
    return importlib.util.module_from_spec(spec)


def _validated_plugin_export(
    module: ModuleType, descriptor: RuntimePluginDescriptor
) -> Plugin[PluginContext]:
    """Require one locally-authored public class and its singleton instance."""

    value = getattr(module, "plugin", None)
    if not isinstance(value, Plugin):
        raise PluginImportError(
            f"runtime plugin {descriptor.name!r} must export Plugin instance 'plugin'"
        )
    plugin_type = type(value)
    if plugin_type is Plugin or plugin_type.__module__ != module.__name__:
        raise PluginImportError(
            f"runtime plugin {descriptor.name!r} instance must use a local Plugin class"
        )
    class_name = plugin_type.__name__
    if (
        class_name.startswith("_")
        or not class_name.isidentifier()
        or keyword.iskeyword(class_name)
        or getattr(module, class_name, None) is not plugin_type
    ):
        raise PluginImportError(
            f"runtime plugin {descriptor.name!r} must publicly export its Plugin class"
        )
    _reject_extra_plugin_exports(module, value, plugin_type, descriptor.name)
    return cast(Plugin[PluginContext], value)


def _reject_extra_plugin_exports(
    module: ModuleType,
    plugin: Plugin[PluginContext],
    plugin_type: type[Plugin[PluginContext]],
    name: str,
) -> None:
    """Prevent ambiguous lifecycle ownership from multiple public plugins."""

    extras = [
        *_extra_plugin_instances(module, plugin),
        *_extra_plugin_classes(module, plugin_type),
    ]
    if extras:
        raise PluginImportError(
            f"runtime plugin {name!r} exports additional Plugin resources: "
            + ", ".join(extras)
        )


def _extra_plugin_instances(
    module: ModuleType, plugin: Plugin[PluginContext]
) -> list[str]:
    """Return public singleton exports competing with the declared plugin."""

    return sorted(
        export
        for export, value in vars(module).items()
        if not export.startswith("_")
        and isinstance(value, Plugin)
        and value is not plugin
    )


def _extra_plugin_classes(
    module: ModuleType, plugin_type: type[Plugin[PluginContext]]
) -> list[str]:
    """Return additional public Plugin subclasses with ambiguous ownership."""

    return sorted(
        export
        for export, value in vars(module).items()
        if not export.startswith("_")
        and inspect.isclass(value)
        and value not in {Plugin, plugin_type}
        and issubclass(value, Plugin)
    )


def _release_activated(plugins: Sequence[ActivatedPlugin]) -> None:
    """Remove child modules in reverse dependency order."""

    for activated in plugins:
        Plugin._clear_identity(activated.plugin, activated.descriptor.name)
        _remove_plugin_modules(activated.descriptor.name)


def _remove_plugin_modules(name: str) -> None:
    """Remove one plugin namespace and every relative import it created."""

    namespace = f"{__name__}.{name}"
    for module_name in tuple(sys.modules):
        if module_name == namespace or module_name.startswith(namespace + "."):
            sys.modules.pop(module_name, None)
    parent = sys.modules.get(__name__)
    if parent is not None and hasattr(parent, name):
        delattr(parent, name)


__all__ = [
    "ActivatedPlugin",
    "Plugin",
    "PluginContext",
    "PluginContextUnavailableError",
    "PluginImportError",
    "PluginNamespaceError",
    "PluginStartContext",
    "activate_runtime_plugins",
    "release_runtime_plugins",
    "runtime_plugin_namespaces",
    "plugin_mutation",
]
